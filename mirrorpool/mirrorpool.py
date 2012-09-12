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
import threading
import subprocess
import ConfigParser
import multiprocessing
from multiprocessing.pool import ThreadPool

GITOLITE_GROUP_NUMBER = 2
GITOLITE_PATTERN = "(\s)+([#RW_]+\s+)+([A-Za-z0-9-_/]+)"
GIT_URL = "default"

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

                banner = ["************************************************",
                          "Use of this system by unauthorized persons or",
                          "in an unauthorized manner is strictly prohibited",]

                for line in [ l for l in stdout.splitlines() if l not in banner ]:
                    self.logger.info(line)
                for line in [ l for l in stderr.splitlines() if l not in banner ]:
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

    cli = get_parser_args()
    start = greetings(cli, logger)
        
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

            results, init_repos = spawn_repos(repo_urls, cli.giturl, cli.repos, cli.repolist, cli.mirrorpool, cli.spawnpath if cli.spawnpath else cli.workingdir, logger, cli.forceserial)

            if cli.initsubmods:
                needs_init = []
                for init_repo in init_repos:
                    if init_repo is not None:
                        needs_init.append(init_repo)
                if len(needs_init):
                    init_submodules(needs_init, cli.spawnpath if cli.spawnpath else cli.workingdir, cli.mirrorpool, cli.forceserial, logger)

        else:
            results = refresh_mirrors(repo_urls, cli.giturl, cli.repos, cli.repolist, cli.mirrorpool, cli.workingdir, logger, cli.forceserial)

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    status = farewells(cli, results, logger, time.time() - start)

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
        repolist = "repolist"

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

def spawn_repos(repo_urls, giturl, repos, repolist, mirrorpool, spawnpath, logger, forceserial):

    logger.info("spawn_repos:")
    #logger.info("repo_urls: \n%s" % repo_urls)
    logger.info("giturl: %s" % giturl)
    logger.info("repos: %s" % repos)
    logger.info("mirrorpool: %s" % mirrorpool)
    logger.info("spawnpath: %s" % spawnpath)
    logger.info("forceserial: %s" % forceserial)

    results = []

    workitems, workcount = pack_workitems(giturl, repos, repolist, mirrorpool, ensure_spawnpath(spawnpath, logger) )

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

    refSpec = "+refs/heads/*:refs/heads/*"

    giturl, repo, width, mirrorpool, spawnpath = workitem

    root, reponame, revision = get_repo_spec(giturl, repo, mirrorpool)

    logger = create_console_logger(reponame, width)
    cmd = LoggingCommand(logger)

    logger.info("spawn_repo: giturl=%s repo=%s width=%s mirrorpool=%s spawnpath=%s" % (giturl, repo, width, mirrorpool, spawnpath) )
    logger.info("spawn_repo: root=%s reponame=%s revision=%s" % (root, reponame, revision) )

    start = time.time()

    if not root:
        root = giturl

    logger.info("giturl=%s root=%s" % (giturl, root) )

    try:
        repopath = spawnpath + '/' + reponame
        logger.info("spawnpath=%s reponame=%s equals repopath=%s" % (spawnpath, reponame, repopath) )

        mirrorpath = create_mirrorpath(mirrorpool, reponame)
        if not os.path.isdir(mirrorpath):
            status, text = create_mirror(None, giturl, reponame, revision, mirrorpool, logger)

        if not status and os.path.isdir(repopath):
            status = cmd.run("rm -rf %s" % repopath)[0]
        status = cmd.run("mkdir -p %s" % (spawnpath), os.getcwd())[0]
        text = "spawn_repo: %s:%s here: %s" % (reponame, revision, spawnpath)

        needs_init = None

        if not status: status = cmd.run("git clone --shared %s %s" % (mirrorpath, repopath), spawnpath)[0]
        if os.path.isdir(repopath):
            if not status: status = ensure_status(cmd, repopath)[0] # this is required in both places, here and below
            if not status: status = cmd.run("git fetch -f -u %s:/%s %s" % (root, reponame, refSpec), repopath)[0]
            if not status: status = cmd.run("git checkout -f %s" % (revision), repopath)[0]
            if not status: status = ensure_status(cmd, repopath)[0] # this is required in both places, here and above
            if not status: status = cmd.run("git remote rm origin", repopath)[0]
            if not status: status = cmd.run("git remote add origin %s:/%s" % (root, reponame), repopath)[0]
            if not status: status = cmd.run("git config branch.%s.remote origin" % (revision), repopath)[0]
            if not status: status = cmd.run("git config branch.%s.merge refs/heads/%s" % (revision, revision), repopath)[0]
            if not status: status = cmd.run("git rev-parse HEAD", repopath)[0]

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


def refresh_mirrors(repo_urls, giturl, repos, repolist, mirrorpool, workingdir, logger, forceserial):

    logger.info("refresh_mirrors:")
    #logger.info("repo_urls: \n%s" % repo_urls)
    logger.info("giturl: %s" % giturl)
    logger.info("repos: %s" % repos)
    logger.info("mirrorpool: %s" % mirrorpool)
    logger.info("workingdir: %s" % workingdir)
    logger.info("forceserial: %s" % forceserial)

    results = []

    workitems, workcount = pack_workitems(giturl, repos, repolist, mirrorpool, workingdir)

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

    giturl, repo, width, mirrorpool, spawnpath = workitem

    root, reponame, revision = get_repo_spec(giturl, repo, mirrorpool)

    logger = create_console_logger(reponame, width)
    cmd = LoggingCommand(logger)

    logger.info("refresh_mirror: giturl=%s repo=%s width=%s mirrorpool=%s spawnpath=%s" % (giturl, repo, width, mirrorpool, spawnpath) )
    logger.info("refresh_mirror: root=%s reponame=%s revision=%s" % (root, reponame, revision) )

    start = time.time()

    if not root: 
        root = mirrorpool

    try:
        mirrorpath = create_mirrorpath(mirrorpool, reponame)
        if os.path.isdir(mirrorpath):
            text = "refresh_mirror: %s here: %s" % (reponame, mirrorpath)
            status = cmd.run("git fetch", mirrorpath)[0]
        else:
            (status, text) = create_mirror(root, giturl, reponame, revision, mirrorpool, logger)

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
    SUBMODPATH_REGEX  = "path = (repos/(closed|open)/(tests/)?([A-Za-z0-9-_./]+))"
    SUBMODURL_REGEX  = "url = [\w\W]*.com/([A-Za-z0-9-_./]+)\n"
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
        
        GIT_INIT = "git submodule init "
        results = []
        if os.path.exists(gitmodules_path):
            submod_workitems, workcount = pack_submodwork(gitmodules_path, mirrorpool, reponame, path)
            for submod_workitem in submod_workitems:
                sub, mirrorsub, mirrorpool, reponame, path, width = submod_workitem
                command = GIT_INIT + " -- " + sub
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
    command = "git submodule update --reference" + create_mirrorpath(mirrorpool, mirrorsub) + " -- " + sub
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

def create_mirror(root, giturl, reponame, revision, mirrorpool, logger):

    print "create_mirror:"
    print "root: ", root
    print "giturl: ", giturl
    print "reponame: ", reponame
    print "revision: ", revision
    print "mirrorpool: ", mirrorpool

    status = 0
    text = ""

    cmd = LoggingCommand(logger)

    try:
        if not is_uri(root):
            root = giturl

        logger.info("creat_mirror:")
        logger.info("root=%s reponame=%s revision=%s mirrorpool=%s" % (root, reponame, revision, mirrorpool) )

        mirrorpath = mirrorpool + '/' + reponame + ".git"

        text = "create_mirror: %s here: %s" % (reponame, mirrorpath)
        status = cmd.run("git clone --mirror %s:/%s %s" % (root, reponame, mirrorpath), mirrorpool)[0]

    except Exception as ex:
        status = -13
        logger.critical(ex)
        traceback.print_exc()

    return status, text

def get_from_conf_or_default(parser, section, key, default):

    value = parser.get(section, key) if parser.has_option(section, key) else default

    if isinstance(value, (list, tuple) ):
        return value
    elif isinstance(value, str):
        value = value.strip()

        value = value.split()

        if len(value) == 0:
            return default
        if len(value) > 1:
            return value
        return value[0]
    return value
    

def load_from_conf(mirrorpool):

    conf = "mirrorpool.conf"
    parser = ConfigParser.ConfigParser()
    parser.read(os.path.join(mirrorpool, conf) )

    giturl = get_from_conf_or_default(parser, conf, 'giturl', None)
    repos = get_from_conf_or_default(parser, conf, 'repos', [])
    spawnpath = get_from_conf_or_default(parser, conf, 'spawnpath', None)

    return giturl, repos, spawnpath

def get_parser_args():

    parser = argparse.ArgumentParser(description='mirrorpool.py options and arguments:')

    parser.add_argument(
        '--mirrorpool', 
        default=".mirrorpool",
        metavar="NAME",
        dest="mirrorpool",
        help="a folder where the mirrorpool is to be setup; defaults to .mirrorpool")
    
    known, remaining = parser.parse_known_args('--mirrorpool')
    giturl, repos, spawnpath = load_from_conf(known.mirrorpool)

    parser.add_argument(
        '--giturl',
        default=giturl,
        dest="giturl",
        help="overrides possible definition of giturl param in %s/mirrorpool.conf" % known.mirrorpool)
    parser.add_argument(
        '--repos',
        default=repos, 
        nargs='+', 
        dest="repos",
        help="relative path to local path or uri to remote server + repo")
    parser.add_argument(
        '--spawn',
        default=None, 
        nargs='?', 
        const=spawnpath if spawnpath else os.getcwd(),
        metavar="PATH",
        dest="spawnpath",
        help="boolean flag for spawning repos local to the current working directory or with PATH specified to the relative PATH from current working dir")
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
        const="repolist", 
        dest="createlist",
        help="creates a repolist with optional name or defaults to %s; calls ssh -T GITURL where GITURL is defined in %s/mirrorpool.conf; can be overridden with --giturl option" % ("repolist", known.mirrorpool) )
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
    parser.add_argument(
        '--forceserial',
        default=False, 
        action="store_true",
        dest="forceserial",
        help="forces the program to run in single process mode; good for debug")

    return parser.parse_args()

def mirrorpool_logo(cli):

    logo = []
    logo.append("                                                                     ")
    logo.append("  -----------------------------------------------------------------  ")
    logo.append(" /MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMN\ ")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMM  mirrorpool.py  MMMMMMMMMMMMMMMMMMMNDNNO|")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMDOO87|")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM$77$+|")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMN$$?II|")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNNI,?~7|")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMZ7:I,$|")
    logo.append("|MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNNNNNNDDDDDDDD888888888888888O:=~,,|")
    logo.append("|MMMMMMMMMMMMNNNNNDDD8888888888888888OOOOOOOOOOOOOOOOOOOOOOOOII:~..,|")
    logo.append("|MNNDDD8D88888888OOOOOOOOOOOOZZZZZZZZZZZZZZZZZZZZ$$$$$$$$$$$O,,:Z,,,|")
    logo.append("|88888OOOOOOOOZZZZZZZZZ$$$$$$$$$$777777777777777I77II7777I?$$~?D++~I|")
    logo.append("|OOOOOZZZZZZ$$$$$7777777IIIIIIII????????????++?$O8OO$7?==~?~:DNO$??7|")
    logo.append("|ZZZZ$$$$$77777IIIII??????+++++++=====~IZOZ7I=:~::,,,,,:::~ONDZZO7?7|")
    logo.append("|$$77777IIII?????++++=====~~~~~~:?ZZOZ+~~:,,,,,:,,,::,,,??DNNZO=,...|")
    logo.append("|77III????++++====~~~~:::::=?I?~~OO+~:,,,:::.::,:,,,:~Z8DDD$7:,.....|")
    logo.append("|II???+++====~~~~::::::??+=+~~~OO7=::~~::::.:,::::~I?I+??7=,,,......|")
    logo.append("|??+++===~~~~~:::::~$?+=~~~~:~?IZ87$$7?::~::77I77I7II?II?~:::::,,...|")
    logo.append("|+++===~~~~::::::IO?=~~~~:::::::??+?+ZZ$$?+I=::+I7777$77I?I?+=~:::,.|")
    logo.append("|+++===~~~::::+$Z+=~~~~:::::,:::::,:=?::+$OZZ7?I7$7I?++IIII+++II??,.|")
    logo.append("|+====~~~::::7?+==~~~~::::,,,::~:,:,,.....:~:,:=?7ZO88OOZ$$I????+,,,|")
    logo.append("|+====~~~~::,?+===~~~:::::,:::~=:.,,:=:,,,,,,,.........,:~~~~~:,,,..|")
    logo.append("|++====~~~:::?===~~~~~:::,,:::~~,,,,,,,,,,,:~===~:::,,:,,,,,,,,,,,,.|")
    logo.append("|++++====~~~~8~I===~~~~::::::::~=.:,,,,,,,,,,,,,,,,,,,,,=?+??=:::,..|")
    logo.append("|???++++===~~~=,~?I===~~~~~~::::::~=~,,,,,,,,,,,,,,,,,::::~~=:.,,,,.|")
    logo.append("|II????++++===~~I:~~+$?====~~~~~~:::~:~==~,,::::::::::,,,,,,,,:::,:,|")
    logo.append("|7IIII?????+++++==~$=~=~~?7?======~~~~~~~~~~~~~+++=~::::~~~~~~~~:~~~|")
    logo.append("|7777IIIII??????+++++==II=====+I7I+=+++=====================+++++???|")
    logo.append("|$$777777IIIIII??????++++++=?I?======+?I7I?++++?++++++++++++++++++++|")
    logo.append("|$$$$$$77777777IIIIIIIIII????????++?II?++++++++++?I777I??++++=,~=~:+|")
    logo.append("|ZZZZ$$$$$$$$$77777777IIIIIIIIIIIII?II????+??+?II7I??++?+???????????|")
    logo.append("|ZZZZZZZZ$$$$$$$$$$$77777777777777IIIIIIIIIIIIIIIIIIIIIII?????????II|")
    logo.append("|OOOOOOZZZZZZZZZZ$$$$$$$$$$$$$$$$77777777777777777777777777777777777|")
    logo.append("|OOOOOOOOOOOZZZZZZZZZZZZZZZZZ$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$|")
    logo.append("|88888OOOOOOOOOOOOOOOOZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ|")
    logo.append("|888888888888OO88OOOOOOOOOOOOOOOOOOOOOOOOOOOOZZZZZZZZZZZZZZZZZZZZZOO|")
    logo.append("|8888888888888888888888888OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO  sai  OO|")
    logo.append(" \DDDDD8D8888888888888888888888888888888OOO88O8OOOOOOOOOOOOOOOOO88O/ ")
    logo.append("  -----------------------------------------------------------------  ")
    logo.append("")
    logo.append("  cmdargs     = " + ' '.join(sys.argv[1:]) )
    logo.append("  mirrorpool  = %s" % cli.mirrorpool)
    logo.append("  giturl      = %s" % cli.giturl)
    logo.append("  repos       = %s" % cli.repos)
    logo.append("  spawnpath   = %s" % cli.spawnpath)
    logo.append("  repolist    = %s" % cli.repolist)
    logo.append("  createlist  = %s" % cli.createlist)
    logo.append("  workingdir  = %s" % cli.workingdir)
    logo.append("  revision    = %s" % cli.revision)
    logo.append("  initsubmods = %s" % cli.initsubmods)
    logo.append("  forceserial = %s" % cli.forceserial)
    logo.append("")

    return logo

def greetings(cli, logger):

    beg = time.time()
    [logger.info(line) for line in mirrorpool_logo(cli)]
    return beg

def farewells(cli, results, logger, duration):

    failures = 0
    [logger.info(line) for line in mirrorpool_logo(cli)]

    if results:
        results.sort(key=lambda r: r[0])
        for (ec, txt) in results:
            if ec:
                logger.info("failure(%d): %s" % (ec, txt) )
                failures+=1
            else:
                logger.info("success(%d): %s" % (ec, txt) )

    logger.info("                                                           ")
    logger.info("completed %d tasks in %f seconds with %d failures          " % (len(results), duration, failures) )
    logger.info("                                                           ")
    logger.info("thank you for using mirrorpool.py --sai *hugs*             ")
    logger.info("***********************************************************")
    logger.info("                                                           ")

    return failures
            
def collect_repos(repos, repolist, mirrorpool):

    print "mirrorpool: ", mirrorpool

    if not repos and not repolist and os.path.exists(mirrorpool):
        repos = [dir for dir in os.listdir(mirrorpool) if os.path.isdir(os.path.join(mirrorpool, dir) ) and dir.endswith('.git')]
    else:
        repo_set = set(repos)
        if repolist and os.path.isfile(repolist):
            repo_file = open(repolist, 'r')
            for line in repo_file.readlines():
                repo_set.add(line.rstrip('\n') )
        repos = list(repo_set)

    print "return from collect_repos with: repos=%s" % repos

    return repos

def collect_repos_old(repos, repolist, mirrorpool):

    print "mirrorpool: ", mirrorpool

    if not repos and not repolist and os.path.exists(mirrorpool):
        print "first branch"
        repos = []
        for top, dirs, files in os.walk(mirrorpool):
            for d in dirs:
                if os.path.isdir(os.path.join(top, d)) and d.endswith('.git'):
                    repos.append(top + "/" + d)
    else:
        print "second branch"
        repo_set = set(repos)
        if repolist and os.path.isfile(repolist):
            repo_file = open(repolist, 'r')
            for line in repo_file.readlines():
                repo_set.add(line.rstrip('\n') )
        repos = list(repo_set)

    print "return from collect_repos with: repos=%s" % repos

    return repos

def get_max_width(giturl, repos, mirrorpool):
    width = 0
    if len(repos):
        width = max([get_reponame_width(giturl, repo, mirrorpool) for repo in repos])
    return width

def ensure_spawnpath(spawnpath, logger):

    if spawnpath:
        spawnpath = unix_path(os.path.abspath(spawnpath) )
        if not os.path.isdir(spawnpath):
            logger.info("making spawnpath: %s" % spawnpath)
            os.mkdir(spawnpath)
    return spawnpath

def ensure_status(cmd, repopath):

    status, text = cmd.run("git status", repopath)

    if not status and not text.endswith('nothing to commit (working directory clean)\n'):

        pattern = "((modified|deleted):\s+)([A-Za-z0-9-_./]+)"
        matches = re.findall(pattern, text)
        files = [match[2] for match in matches]

        text = ""
        for file in files:
            s, t = cmd.run("git update-index --assume-unchanged %s" % file, repopath)
            status += s
            text += t

    return status, text

def pack_workitems(giturl, repos, repolist, mirrorpool, workingdir):

    mirrorpool = unix_path(os.path.abspath(mirrorpool) )
    repos = collect_repos(repos, repolist, mirrorpool)
    width = get_max_width(giturl, repos, mirrorpool)
    workitems = [(giturl, repo, width, mirrorpool, workingdir) for repo in repos]
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

def get_repo_spec(giturl, repo, mirrorpool):
  
    pattern = "((__MIRRORPOOL__|(ssh://)?([A-Za-z0-9\-_.]+)(@)([A-Za-z0-9\-_.]+))(:/|/))?([A-Za-z0-9\-_/]+)(.git|)((:)([A-Za-z0-9\-_]+))?".replace("__MIRRORPOOL__", mirrorpool)
    match = re.search(pattern, unix_path(repo) )
    root = match.group(2)
    reponame = match.group(8)
    revision = match.group(12)

    return root, reponame, revision if revision else "master"

def get_reponame_width(giturl, repo, mirrorpool):
    width = 0
    root, reponame, revision = get_repo_spec(giturl, repo, mirrorpool)

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

    pattern = "([a-z0-9-_]+)(@)([a-z0-9-_.]+)"
    return repostring != None and re.match(pattern, repostring) != None

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
