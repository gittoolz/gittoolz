#!/usr/bin/env python
#
#
#
__author__ = 'saidler'

import os
import re
import sys
import time
import cgitb
import shutil
import logging
import datetime
import argparse
import traceback
import subprocess
import threading
import multiprocessing
from multiprocessing.pool import ThreadPool

MULTIPLIER = 2
REPOLIST = "repolist"
LOGNAME = "mirrorpool"
GIT = "git"
REF_SPEC = "+refs/heads/*:refs/heads/*"
REPO_SPEC_PATTERN = "((^[A-Za-z0-9-_/~@.]+)(:?/))?(([A-Za-z0-9-_/]+)(\.git)?)((:)([A-Za-z0-9-_]+$))?"

REPO_ISURI_PATTERN = "([a-z0-9-_]+)(@)([a-z0-9-_.]+)"
ROOT_GROUP_NUMBER = 2
REPONAME_GROUP_NUMBER = 5
REVISION_GROUP_NUMBER = 9
REPO_PATH_GROUP_NUMBER = 4

GITOLITE_PATTERN = "(\s)+([#RW_]+\s+)+([A-Za-z0-9-_/]+)"
GITOLITE_GROUP_NUMBER = 2

MODIFIED_OR_DELETED_PATTERN = "((modified|deleted):\s+)([A-Za-z0-9-_./]+)"
MODIFIED_OR_DELETED_GROUP_NUMBER = 2

SSH_BANNER_SPAM_LIST = ["************************************************",
                        "Use of this system by unauthorized persons or",
                        "in an unauthorized manner is strictly prohibited",]

SUBMODPATH_REGEX  = "path = (repos/(closed|open)/(tests/)?([A-Za-z0-9-_./]+))"
SUBMODURL_REGEX  = "url = [\w\W]*.com/([A-Za-z0-9-_./]+)\n"
GIT_INIT_SUBS = "git submodule update --init --reference "
GIT_INIT = "git submodule init "
GIT_UPDATE = "git submodule update --reference "
GIT_EXT = ".git"
SPACE = " "
DASH = "--"

def verbose_exception(exc_type, exc_value, exc_traceback):
    message = cgitb.text((exc_type, exc_value, exc_traceback))
    print message
    sys.exit(1)

sys.excepthook = verbose_exception

class ConsoleLogger(logging.Logger):
    def __init__(self, name, level):
        Logger(name, level)

class LoggingCommand():
    def __init__(self, logger, workingdir = "."):
        self.logger = logger
        self.workingdir = workingdir

    def run(self, command, workingdir = None):
        status = 0
        text = ""
        if not command:
            self.logger.error("Empty cmdargs passed to run_command")
        else:
            if not workingdir:
                workingdir = self.workingdir
                
            self.logger.info("running command: %s/%s" % (workingdir, command) )


            doShell=False #perhaps parameterize later?


            try:
                process = subprocess.Popen(
                    command if doShell else command.split(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workingdir,
                    shell=doShell)

                stdout, stderr = process.communicate()

                for line in [ l for l in stdout.splitlines() if l not in SSH_BANNER_SPAM_LIST ]:
                    self.logger.info(line)
                for line in [ l for l in stderr.splitlines() if l not in SSH_BANNER_SPAM_LIST ]:
                    self.logger.error(line)


                text = os.linesep.join([stdout, stderr]) 
                status = process.returncode

            except Exception as ex:
                self.logger.critical(ex)
                traceback.print_exc()

        return status, text

def main():

    status = 0
    logger = create_console_logger("mirrorpool")
    cmd = LoggingCommand(logger)
    start = greetings(logger)

    cli = get_parser_args()
        
    os.chdir(cli.workingdir)
    repos = cli.repos

    try:

        workcount = 0
        results = []
        repo_urls = []

        status, response = cmd.run("ssh -T %s" % cli.giturl)
        matches = re.findall(GITOLITE_PATTERN, response)    
        repo_urls = ["%s:/%s" % (cli.giturl, match[GITOLITE_GROUP_NUMBER]) for match in matches]

        logger.info("making mirrorpool: %s" % cli.mirrorpool)
        status = cmd.run("mkdir -p %s" % cli.mirrorpool, os.getcwd())
        
        if cli.createlist:
            results = create_repolist(repo_urls, cli.createlist)
        elif cli.spawnpath:

            results, init_repos = spawn_repos(repo_urls, cli.repos, cli.repolist, cli.mirrorpool, cli.spawnpath if cli.spawnpath else cli.workingdir, logger, cli.forceserial)

            if cli.initsubmods:
                needs_init = []
                for init_repo in init_repos:
                    if init_repo is not None:
                        needs_init.append(init_repo)
                if len(needs_init):
                    init_submodules(needs_init, cli.spawnpath if cli.spawnpath else cli.workingdir, cli.mirrorpool, cli.forceserial, logger)

        else:
            results = refresh_mirrors(repo_urls, cli.repos, cli.repolist, cli.mirrorpool, cli.workingdir, logger, cli.forceserial)

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    status = farewells(results, logger, time.time() - start)

    sys.exit(status)

def get_repo_urls(giturl):

    status = 0
    text = ""
    repo_urls = []

    try:
        status, response = cmd.run("ssh -T %s" % giturl)

        matches = re.findall(GITOLITE_PATTERN, response)    
        repo_urls = ["%s:/%s" % (giturl, match[GITOLITE_GROUP_NUMBER]) for match in matches]

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    return repo_urls

def create_repolist(repo_urls, repolist):

    status = 0
    text = ""

    logger = create_console_logger("createlist")
    cmd = LoggingCommand(logger)

    if not repolist:
        repolist = REPOLIST

    if os.path.isfile(repolist):
        now = datetime.datetime.now()
        newfile = now.strftime(".%m-%d-%y_%I.%M.%S%p.bkup")
        shutil.move(repolist, repolist + newfile)

    try:

        f = open(repolist, 'w')

        for repo_url in repo_urls:
            f.write("%s\n" % repo_url)
            logger.info("%s added to repolist" % repo_url)

        f.close()

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    return [(status, text)]

def spawn_repos(repo_urls, repos, repolist, mirrorpool, spawnpath, logger, forceserial):

    results = []

    workitems, workcount = pack_workitems(repos, repolist, mirrorpool, ensure_spawnpath(spawnpath, logger) )


    logger.info("")

    pool = None
    if not forceserial:
        pool, processcount = get_multiprocessing_pool(workcount, logger)
    if pool:
        logger.info("running mirrorpool.py in multi-process mode with a pool size of %d" % processcount)
        results = pool.map(spawn_repo, workitems)
    else:
        logger.info("PUNT!  running mirrorpool.py in serial mode because multiprocessing failed...")
        results = [spawn_repo(workitem) for workitem in workitems]

    logger.info("")


    return [(result[0], result[1]) for result in results], [(result[2]) for result in results]


def spawn_repo(workitem):

    status = 0
    text = ""

    repo, width, mirrorpool, spawnpath = workitem

    root, reponame, revision = get_repo_spec(repo, mirrorpool)

    logger = create_console_logger(reponame, width)
    cmd = LoggingCommand(logger)

    start = time.time()

    if not root:
        root = GIT_URL

    try:
        repopath = spawnpath + '/' + reponame

        mirrorpath = create_mirrorpath(mirrorpool, reponame)
        if not os.path.isdir(mirrorpath):
            status, text = create_mirror(root, reponame, revision, mirrorpool, logger)

        if not status and os.path.isdir(repopath):
            status = cmd.run("rm -rf %s" % repopath)[0]
        status = cmd.run("mkdir -p %s" % (spawnpath), os.getcwd())[0]
        text = "spawn_repo: %s:%s here: %s" % (reponame, revision, spawnpath)

        needs_init = None

        if not status: status = cmd.run("%s clone --shared %s %s" % (GIT, mirrorpath, repopath), spawnpath)[0]
        if os.path.isdir(repopath):
            if not status: status = ensure_status(cmd, repopath)[0] # this is required in both places, here and below
            if not status: status = cmd.run("%s fetch -f -u %s:/%s %s" % (GIT, GIT_URL, reponame, REF_SPEC), repopath)[0]
            if not status: status = cmd.run("%s checkout -f %s" % (GIT, revision), repopath)[0]
            if not status: status = ensure_status(cmd, repopath)[0] # this is required in both places, here and above
            if not status: status = cmd.run("%s remote rm origin" % GIT, repopath)[0]
            if not status: status = cmd.run("%s remote add origin %s:/%s" % (GIT, GIT_URL, reponame), repopath)[0]
            if not status: status = cmd.run("%s config branch.%s.remote origin" % (GIT, revision), repopath)[0]
            if not status: status = cmd.run("%s config branch.%s.merge refs/heads/%s" % (GIT, revision, revision), repopath)[0]
            if not status: status = cmd.run("%s rev-parse HEAD" % GIT, repopath)[0]

            gitmodules_path = os.path.join(repopath, ".gitmodules")
            if os.path.exists(gitmodules_path):
                needs_init = reponame

        else:
            logger.critical("status=%d; initial clone failed; remaining spawn_repo tasks unable to execute..." % status)
            status = -39

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    text += "; completed in %f seconds" % (time.time() - start)
    

    return status, text, needs_init


def refresh_mirrors(repo_urls, repos, repolist, mirrorpool, workingdir, logger, forceserial):

    results = []


    workitems, workcount = pack_workitems(repos, repolist, mirrorpool, workingdir)

    logger.info("")

    pool = None
    if not forceserial:
        pool, processcount = get_multiprocessing_pool(workcount, logger)
    if pool:
        logger.info("running mirrorpool.py in multi-process mode with a pool size of %d" % processcount)
        results = pool.map(refresh_mirror, workitems)
    else:
        logger.info("PUNT!  running mirrorpool.py in serial mode because multiprocessing failed...")
        results = [refresh_mirror(workitem) for workitem in workitems]

    logger.info("")

    return results

def refresh_mirror(workitem):
    
    status = 0
    text = ""


    repo, width, mirrorpool, spawnpath = workitem

    root, reponame, revision = get_repo_spec(repo, mirrorpool)

    logger = create_console_logger(reponame, width)
    cmd = LoggingCommand(logger)

    start = time.time()

    if not root: 
        root = mirrorpool

    try:
        print reponame
        mirrorpath = create_mirrorpath(mirrorpool, reponame)
        if os.path.isdir(mirrorpath):
            text = "refresh_mirror: %s here: %s" % (reponame, mirrorpath)
            status = cmd.run("%s fetch" % GIT, mirrorpath)[0]
        else:
            (status, text) = create_mirror(root, reponame, revision, mirrorpool, logger)

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    text += "; completed in %f seconds" % (time.time() - start)

    return status, text

def get_submodules(gitmodules):
    f = open(gitmodules, "r")
    raw_modules = f.read()
    modules =  raw_modules.split("[")
    submods = {}
    for mod in modules:
        if mod:
            submodpath_re = re.compile(SUBMODPATH_REGEX)
            submodpath_match = submodpath_re.search(mod)
            path = submodpath_match.group(1)
            submodurl_re = re.compile(SUBMODURL_REGEX)
            submodurl_match = submodurl_re.search(mod)
            url = submodurl_match.group(1)
            submods[path] = url
    f.close()
    print submods
    return submods


def init_submodules(reponames, path, mirrorpool, forceserial, logger):
    cmd = LoggingCommand(logger)
    print mirrorpool
    for reponame in reponames:
        gitmodules_path = os.path.join(path, reponame, ".gitmodules")
        
        results = []
        if os.path.exists(gitmodules_path):
            submod_workitems, workcount = pack_submodwork(gitmodules_path, mirrorpool, reponame, path)
            for submod_workitem in submod_workitems:
                sub, mirrorsub, mirrorpool, reponame, path, width = submod_workitem
                command = GIT_INIT + SPACE + DASH + SPACE + sub
                cmd.run(command, os.path.join(path, reponame))
            
            pool = None
            if not forceserial:
                pool, processcount = get_multiprocessing_pool(workcount, logger)
            if pool:
                logger.info("running mirrorpool.py in multi-process mode with a pool size of %d" % processcount)
                results = pool.map(init_submodule, submod_workitems)
            else:
                logger.info("PUNT!  running mirrorpool.py in serial mode because multiprocessing failed...")
                results = [init_submodule(submod_workitem) for submod_workitem in submod_workitems]

def init_submodule(workitem):
    
    sub, mirrorsub, mirrorpool, reponame, path, width = workitem
    text = "initializing %s" % sub
    logger = create_console_logger(sub, width)
    cmd = LoggingCommand(logger)
    status = 0
    command = GIT_UPDATE + create_mirrorpath(mirrorpool, mirrorsub) + SPACE + DASH + SPACE + sub
    runpath = os.path.join(path, reponame)
    status = cmd.run(command, runpath)[0]
    return status, text

def pack_submodwork(gitmodules_path, mirrorpool, reponame, path):
    mirrorpool = unix_path(os.path.abspath(mirrorpool) )
    submods = get_submodules(gitmodules_path)
    width = get_max_submod_width(submods, mirrorpool)
    submod_workitems = [(sub, submods[sub], mirrorpool, reponame, path, width) for sub in submods.keys()]
    return submod_workitems, len(submod_workitems) 

def get_max_submod_width(submods, mirrorpool):
    width = 0
    if len(submods):
        width = max([len(submod) for submod in submods])
    return width

def create_mirror(root, reponame, revision, mirrorpool, logger):

    status = 0
    text = ""

    cmd = LoggingCommand(logger)

    try:
        if not is_uri(root):
            root = GIT_URL

        mirrorpath = mirrorpool + '/' + reponame + ".git"

        text = "create_mirror: %s here: %s" % (reponame, mirrorpath)
        status = cmd.run("%s clone --mirror %s:/%s %s" % (GIT, root, reponame, mirrorpath), mirrorpool)[0]

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    return status, text

def get_parser_args():

    parser = argparse.ArgumentParser(description='mirrorpool.py options and arguments:')

    parser.add_argument(
        '--forceserial',
        default=False, 
        action="store_true",
        dest="forceserial",
        help="forces the program to run in single process mode; good for debug")
    parser.add_argument(
        '--repos',
        default=[], 
        nargs='+', 
        dest="repos",
        help="relative path to local path or uri to remote server + repo")
    parser.add_argument(
        '--repolist',
        default=None,
        metavar="NAME",
        dest='repolist',
        help="a file that a repo listing per line in a file")
    parser.add_argument(
        '--createlist', 
        default=None,
        metavar="NAME",
        nargs='?', 
        const=REPOLIST, 
        dest="createlist",
        help="creates a repolist with optional name or defaults to %s; calls ssh -T git@GITURL where GITURL defaults to %s; can be overridden with --giturl option" %(REPOLIST, GIT_URL) )
    parser.add_argument(
        '--mirrorpool', 
        default="~/.mirrorpool",
        metavar="NAME",
        dest="mirrorpool",
        help="a folder where the mirrorpool is to be setup; defaults to ~/mirrorpool")
    parser.add_argument(
        '--spawn',
        default=None, 
        nargs='?', 
        const=os.getcwd(),
        metavar="PATH",
        dest="spawnpath",
        help="boolean flag for spawning repos local to the current working directory")
    parser.add_argument(
        '--workingdir', 
        default=os.getcwd(),
        metavar="PATH",
        dest="workingdir",
        help="the working dir used for refreshes and spawns")
    parser.add_argument(
        '--revision',
        default="master", 
        dest="revision",
        help="the revision used in spawn; defaults to master; is overrided by repo specification repo:version")
    parser.add_argument(
        '--giturl',
        default=GIT_URL,
        dest="giturl",
        help="override the default giturl of %s" % GIT_URL)
    parser.add_argument(
        '--version', 
        default=None, 
        action='version', 
        version='%(prog)s 1.0',
        help="version of this script")
    parser.add_argument(
        '--initsubmods',
        default=False, 
        action="store_true",
        dest="initsubmods",
        help="if one of the specified repos contains submodules, initialize them")

    return parser.parse_args()

def mirrorpool_logo():

    logo = []
    logo.append("                                                                     ")
    logo.append("*********************************************************************")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNM*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMM  mirrorpool.py  MMMMMMMMMMMMMMMMMMMNDNNO*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMDOO87*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM$77$+*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMN$$?II*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNNI,?~7*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMZ7:I,$*")
    logo.append("*MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNNNNNNDDDDDDDD888888888888888O:=~,,*")
    logo.append("*MMMMMMMMMMMMNNNNNDDD8888888888888888OOOOOOOOOOOOOOOOOOOOOOOOII:~..,*")
    logo.append("*MNNDDD8D88888888OOOOOOOOOOOOZZZZZZZZZZZZZZZZZZZZ$$$$$$$$$$$O,,:Z,,,*")
    logo.append("*88888OOOOOOOOZZZZZZZZZ$$$$$$$$$$777777777777777I77II7777I?$$~?D++~I*")
    logo.append("*OOOOOZZZZZZ$$$$$7777777IIIIIIII????????????++?$O8OO$7?==~?~:DNO$??7*")
    logo.append("*ZZZZ$$$$$77777IIIII??????+++++++=====~IZOZ7I=:~::,,,,,:::~ONDZZO7?7*")
    logo.append("*$$77777IIII?????++++=====~~~~~~:?ZZOZ+~~:,,,,,:,,,::,,,??DNNZO=,...*")
    logo.append("*77III????++++====~~~~:::::=?I?~~OO+~:,,,:::.::,:,,,:~Z8DDD$7:,.....*")
    logo.append("*II???+++====~~~~::::::??+=+~~~OO7=::~~::::.:,::::~I?I+??7=,,,......*")
    logo.append("*??+++===~~~~~:::::~$?+=~~~~:~?IZ87$$7?::~::77I77I7II?II?~:::::,,...*")
    logo.append("*+++===~~~~::::::IO?=~~~~:::::::??+?+ZZ$$?+I=::+I7777$77I?I?+=~:::,.*")
    logo.append("*+++===~~~::::+$Z+=~~~~:::::,:::::,:=?::+$OZZ7?I7$7I?++IIII+++II??,.*")
    logo.append("*+====~~~::::7?+==~~~~::::,,,::~:,:,,.....:~:,:=?7ZO88OOZ$$I????+,,,*")
    logo.append("*+====~~~~::,?+===~~~:::::,:::~=:.,,:=:,,,,,,,.........,:~~~~~:,,,..*")
    logo.append("*++====~~~:::?===~~~~~:::,,:::~~,,,,,,,,,,,:~===~:::,,:,,,,,,,,,,,,.*")
    logo.append("*++++====~~~~8~I===~~~~::::::::~=.:,,,,,,,,,,,,,,,,,,,,,=?+??=:::,..*")
    logo.append("*???++++===~~~=,~?I===~~~~~~::::::~=~,,,,,,,,,,,,,,,,,::::~~=:.,,,,.*")
    logo.append("*II????++++===~~I:~~+$?====~~~~~~:::~:~==~,,::::::::::,,,,,,,,:::,:,*")
    logo.append("*7IIII?????+++++==~$=~=~~?7?======~~~~~~~~~~~~~+++=~::::~~~~~~~~:~~~*")
    logo.append("*7777IIIII??????+++++==II=====+I7I+=+++=====================+++++???*")
    logo.append("*$$777777IIIIII??????++++++=?I?======+?I7I?++++?++++++++++++++++++++*")
    logo.append("*$$$$$$77777777IIIIIIIIII????????++?II?++++++++++?I777I??++++=,~=~:+*")
    logo.append("*ZZZZ$$$$$$$$$77777777IIIIIIIIIIIII?II????+??+?II7I??++?+???????????*")
    logo.append("*ZZZZZZZZ$$$$$$$$$$$77777777777777IIIIIIIIIIIIIIIIIIIIIII?????????II*")
    logo.append("*OOOOOOZZZZZZZZZZ$$$$$$$$$$$$$$$$77777777777777777777777777777777777*")
    logo.append("*OOOOOOOOOOOZZZZZZZZZZZZZZZZZ$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$*")
    logo.append("*88888OOOOOOOOOOOOOOOOZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ*")
    logo.append("*888888888888OO88OOOOOOOOOOOOOOOOOOOOOOOOOOOOZZZZZZZZZZZZZZZZZZZZZOO*")
    logo.append("*8888888888888888888888888OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO  sai  OO*")
    logo.append("*DDDDDD8D8888888888888888888888888888888OOO88O8OOOOOOOOOOOOOOOOO88OO*")
    logo.append("*********************************************************************")
    logo.append("* cmdargs: " + ' '.join(sys.argv) )
    logo.append("*********************************************************************")
    logo.append("                                                                     ")

    return logo

def greetings(logger):

    beg = time.time()
    [logger.info(line) for line in mirrorpool_logo()]
    return beg

def farewells(results, logger, duration):

    failures = 0
    [logger.info(line) for line in mirrorpool_logo()]
    if results:
        results.sort(key=lambda r: r[0])
        for (ec, txt) in results:
            if ec:
                logger.info("failure(%d): %s" % (ec, txt) )
                failures+=1
            else:
                logger.info("success(%d): %s" % (ec, txt) )

    logger.info("completed %d tasks in %f seconds with %d failures          " % (len(results), duration, failures) )
    logger.info("                                                           ")
    logger.info("thank you for using mirrorpool.py --sai *hugs*             ")
    logger.info("***********************************************************")
    logger.info("                                                           ")

    return failures
            
def collect_repos(repos, repolist, mirrorpool):

    if not repos and not repolist and os.path.exists(mirrorpool):
        repos = []
        for top, dirs, files in os.walk(mirrorpool):
            for d in dirs:
                if os.path.isdir(os.path.join(top, d)) and d.endswith('.git'):
                    repos.append(top + "/" + d)
    else:
        repo_set = set(repos)
        if repolist and os.path.isfile(repolist):
            repo_file = open(repolist, 'r')
            for line in repo_file.readlines():
                repo_set.add(line.rstrip('\n') )
        repos = list(repo_set)

    return repos

def get_max_width(repos, mirrorpool):
    width = 0
    if len(repos):
        width = max([get_reponame_width(repo, mirrorpool) for repo in repos])
    return width

def pack_workitems_old(repos, mirrorpool, workingdir):

    width = get_max_width(repos, mirrorpool)
    workitems = [(repo, width, mirrorpool, workingdir) for repo in repos]
    return workitems

def ensure_spawnpath(spawnpath, logger):

    if spawnpath:
        spawnpath = unix_path(os.path.abspath(spawnpath) )
        if not os.path.isdir(spawnpath):
            logger.info("making spawnpath: %s" % spawnpath)
            os.mkdir(spawnpath)
    return spawnpath

def ensure_status(cmd, repopath):

    status, text = cmd.run("%s status" % GIT, repopath)

    if not status and not text.endswith('nothing to commit (working directory clean)\n'):

        matches = re.findall(MODIFIED_OR_DELETED_PATTERN, text)
        files = [match[MODIFIED_OR_DELETED_GROUP_NUMBER] for match in matches]

        text = ""
        for file in files:
            s, t = cmd.run("%s update-index --assume-unchanged %s" %(GIT, file), repopath)
            status += s
            text += t

    return status, text

def pack_workitems(repos, repolist, mirrorpool, workingdir):

    mirrorpool = unix_path(os.path.abspath(mirrorpool) )
    repos = collect_repos(repos, repolist, mirrorpool)
    width = get_max_width(repos, mirrorpool)
    workitems = [(repo, width, mirrorpool, workingdir) for repo in repos]
    return workitems, len(workitems)

def get_multiprocessing_pool(workcount, logger):

    pool = None
    processcount = get_processcount(workcount)

    while (processcount > 1):
        logger.info("trying processcount=%d" % processcount)
        try:
            pool = multiprocessing.Pool(processcount)
            break
        except OSError, exc:
            logger.info("exc %s" % exc)
            processcount/= 2
    return pool, processcount

def get_repo_spec(repo, mirrorpool):
    match = re.search(REPO_SPEC_PATTERN, unix_path(repo) )
    root = match.group(ROOT_GROUP_NUMBER)
    reponame = match.group(REPONAME_GROUP_NUMBER)
    revision = match.group(REVISION_GROUP_NUMBER)

    # the first part of the path (eg yocto) will come out as part of the root,
    # so you need to add it in to the reponame
    if root and root.startswith(mirrorpool):
        mirrorpool_end = root.find(mirrorpool) + len(mirrorpool)
        reponame = root[mirrorpool_end+1:] + "/" + reponame
    elif root and not root.startswith(GIT_URL):
        reponame = root + "/" + reponame
    return root, reponame, revision if revision else "master"

def get_reponame_width(repo, mirrorpool):
    width = 0
    root, reponame, revision = get_repo_spec(repo, mirrorpool)
    width = len(reponame)
    if root:
        width += len(root) + 1 # for the slash
    return width

def unix_path(path):
    return path.replace('\\', '/')

def create_mirrorpath(mirrorpool, reponame):
    mirrorname =  reponame + ".git"
    return mirrorpool + '/' + mirrorname # purposefully avoid window pathing to get correct behavior in cygwin environment

def is_uri(repostring):

    return repostring != None and re.match(REPO_ISURI_PATTERN, repostring) != None

def get_processcount(work_count, mulitplier = 1):

    return int(min(multiprocessing.cpu_count(), work_count) * mulitplier )

def create_console_logger(name, name_width = None, level_width = None):

    if not name_width:
        name_width = len(name)

    if not level_width:
        level_width = len("CRITICAL")

    # create formatter
    formatter = logging.Formatter("%(levelname)-" + str(level_width) + "s | %(name)-" + str(name_width) + "s | %(message)s")

    # create logger
    logger = logging.getLogger(name)

    if not logger:
        print "COULDN'T INSTANTIATE LOGGER!!!"
        sys.exit(666)

    logger.setLevel(logging.INFO)

    # create console_handler and set level to debug
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    #logname = get_logname(os.getpid() )
    #file_handler = logging.FileHandler(logname, mode='a')
    #file_handler.setLevel(logging.INFO)

    # add formatter to console_handler
    console_handler.setFormatter(formatter)
    #file_handler.setFormatter(formatter)

    # add console_handler to logger
    logger.addHandler(console_handler)
    #logger.addHandler(file_handler)

    return logger

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
