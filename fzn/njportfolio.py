from utils import print_commented_fzn, total_seconds
import subprocess as sp
import datetime
import signal
import threading
import os
import sys


result_poll_timeout = 0.5
solver_buffer_time = 0.5  # Tell each solver to finish this many seconds ahead of our actual timeout.
SATISFACTION, MINIMIZE, MAXIMIZE = 0, 1, -1
UNKNOWN, SAT, UNSAT = 0, 1, 2


class SolverResult(object):

    def __init__(self, stdout, obj_factor=MINIMIZE):
        self.stdout = stdout
        self.sat = UNKNOWN
        self.opt = False
        self.objective = sys.maxint

        for line in stdout.split("\n"):
            bits = line.strip().split()
            if "=====UNSATISFIABLE=====" in line:
                self.sat = UNSAT
            elif "----------" in line:
                self.sat = SAT
            elif self.sat and "==========" in line:
                self.opt = True
            elif "% Objective" in line or "%  OBJECTIVE" in line:
                self.objective = int(bits[-1]) * obj_factor

    def __lt__(self, other):
        return (self.sat and not other.sat) or \
               (self.opt and not other.opt) or \
               (self.objective < other.objective)


def run_cmd(process_name, starttime, pid_queue, result_queue, cmd, memlimit):
    if memlimit:
        cmd = "ulimit -v %d; %s" % (memlimit, cmd)
    process = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE,
                       shell=True, preexec_fn=os.setpgrp)
    # Tell the parent the process pid, so it can be killed later
    pid_queue.put(process.pid, True)
    stdout, stderr = process.communicate()
    exitcode = process.returncode
    try:
        if exitcode == 0:
            result_queue.put([True, exitcode, process_name, starttime, stdout, stderr], True)
        else:
            result_queue.put([False, exitcode, process_name, starttime, stdout, stderr], True)
    except Exception:
        pass


def check_optimization(njfilename):
    import re
    import mmap

    ret = SATISFACTION
    r = re.compile(r'model\.add\([ ]*(?P<opt>(Maximize|Minimize))\(')
    with open(njfilename, "r+") as f:
        mm = mmap.mmap(f.fileno(), 0)  # Memory map the file in case its big.
        m = r.search(mm)
        if m:
            opt = m.groupdict()["opt"]
            if opt == "Maximize":
                ret = MAXIMIZE
            elif opt == "Minimize":
                ret = MINIMIZE
    return ret


def njportfolio(njfilename, cores, timeout, memlimit):
    from multiprocessing import Queue, cpu_count
    from Queue import Empty

    start_time = datetime.datetime.now()
    result_queue = Queue()
    pid_queue = Queue()
    threads = []

    configs = []
    configs.append({'solver': 'Mistral', 'var': 'DomainOverWLDegree', 'val': 'Lex'})
    configs.append({'solver': 'Gurobi'})
    configs.append({'solver': 'Toulbar2'})
    configs.append({'solver': 'Mistral', 'var': 'Impact', 'val': 'Impact', 'restart': 0, 'base': 10000, 'factor': 1.5})
    configs.append({'solver': 'MiniSat'})
    configs.append({'solver': 'Mistral', 'var': 'DomainOverWDegree', 'val': 'Lex'})
    configs.append({'solver': 'Mistral', 'var': 'Impact', 'val': 'Impact', 'restart': 1, 'base': 256, 'factor': 1.5})
    configs.append({'solver': 'Toulbar2', 'btd': 3, 'lcLevel': 1, 'rds': 1})
    configs.append({'solver': 'SCIP'})
    configs.append({'solver': 'Mistral', 'var': 'Impact', 'val': 'Impact', 'restart': 1, 'base': 512, 'factor': 2})
    configs.append({'solver': 'Mistral', 'var': 'Impact', 'val': 'Impact', 'restart': 0, 'base': 5000, 'factor': 1.5})
    configs.append({'solver': 'Mistral', 'var': 'Impact', 'val': 'Impact', 'restart': 1, 'base': 512, 'factor': 1.3})
    configs.append({'solver': 'Mistral', 'var': 'Impact', 'val': 'Impact', 'restart': 0, 'base': 1000, 'factor': 1.3})
    configs.append({'solver': 'Mistral', 'var': 'DomainOverWDegree', 'val': 'Lex', 'restart': 1, 'base': 256, 'factor': 1.5})
    configs.reverse()  # Reverse the list so we can just pop().

    if cores <= 0 or cores > cpu_count():
        cores = cpu_count()

    def start_new():
        if not configs:
            return  # Could launch Mistral with different seeds if we run out of provided configs
        config = configs.pop()
        remaining_time = int(timeout - total_seconds(datetime.datetime.now() - start_time) - solver_buffer_time)
        defaults = {'njfilename': njfilename, 'threads': 1, 'tcutoff': remaining_time, 'var': 'DomainOverWDegree', 'val': 'Lex', 'verbose': 0, 'restart': 1, 'base': 256, 'factor': 1.3, 'lcLevel': 4, 'lds': 0, 'dee': 0, 'btd': 0, 'rds': 0}
        d = dict(defaults.items() + config.items())
        cmd = ("python %(njfilename)s -solver %(solver)s -tcutoff %(tcutoff)d "
               "-threads %(threads)d -var %(var)s -val %(val)s "
               "-restart %(restart)d -base %(base)d -factor %(factor).1f -verbose %(verbose)d -lds %(lds)d -btd %(btd)d -rds %(rds)d -dee %(dee)d -lcLevel %(lcLevel)d" % d)
        args = (str(config), datetime.datetime.now(), pid_queue, result_queue, cmd, int(memlimit / cores))
        thread = threading.Thread(target=run_cmd, args=args)
        threads.append(thread)
        thread.start()
        print "% Launching:", cmd

    def tidy_up(*args):
        # Tidy up, kill all processes that did not finish.
        num_pids_seen = 0
        while not pid_queue.empty() and num_pids_seen < len(threads):
            try:
                pid = pid_queue.get()
                num_pids_seen += 1
                os.killpg(pid, signal.SIGKILL)
            except Empty:
                pass
            except OSError:
                pass  # Process already finished.

    signal.signal(signal.SIGTERM, tidy_up)
    signal.signal(signal.SIGINT, tidy_up)

    for i in xrange(cores):
        start_new()

    objective_type = check_optimization(njfilename)

    num_finished = 0
    finished_names = []
    results = []
    while True:
        if total_seconds(datetime.datetime.now() - start_time) + result_poll_timeout + 0.1 >= timeout:
            break

        try:
            success, exitcode, process_name, solversstartt, stdout, stderr = \
                result_queue.get(True, result_poll_timeout)
            num_finished += 1
            finished_names.append(process_name)
            if success:
                started_after = total_seconds(solversstartt - start_time)
                timetaken = total_seconds(datetime.datetime.now() - solversstartt)
                res = SolverResult(stdout, objective_type)
                print "%% Solver %s started after %.1f, finished %.1f. objective: %d" \
                    % (process_name, started_after, timetaken, res.objective * objective_type)
                if not objective_type:
                    print stdout
                    break
                else:
                    results.append(res)
                    if res.opt:
                        break
                    # If not optimal, wait for further result to come in until timeout almost exceeded.
            else:
                print "%% Failed: %s exitcode: %d" % (process_name, exitcode)
                print_commented_fzn(stdout)
                print_commented_fzn(stderr)
                start_new()

            if num_finished == len(threads):
                break
        except Empty:
            pass  # Nothing new posted to the result_queue yet.
        except IOError:
            break  # Can happen if sent term signal.
        except KeyboardInterrupt:
            break

    if results:
        print min(results).stdout  # Print the best solution

    tidy_up()
    print "%% Total time in njportfolio: %.1f" % total_seconds(datetime.datetime.now() - start_time)


if __name__ == '__main__':
    if len(sys.argv) != 5:
        print >> sys.stderr, "Usage: python %s njfilename cores timeout memlimit" % sys.argv[0]
        sys.exit(1)
    njportfolio(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]))