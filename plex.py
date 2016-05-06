import Queue
import sys
import threading
import os
import time
from itertools import cycle, chain

import click
import tabulate
import tmuxp
import tmuxp.exc
import yaml
import tempfile


queue = Queue.Queue()
parenthesize = '({})'.format
V = click.style(u'\u2713', fg='green')
X = click.style('X', fg='red')


def fmt_time(s):
    hours, remainder = divmod(s, 3600)
    minutes, seconds = divmod(remainder, 60)
    return '{:02.0f}:{:02.0f}:{:02.0f}'.format(hours, minutes, seconds)


def get_window(env):
    s = tmuxp.Server()
    session = s.getById('$' + os.environ['TMUX'].split(',')[-1])
    if session is None:
        raise ValueError('session can not be None')

    for k, v in env.iteritems():
        s.set_environment(k, v)
    return session.attached_window()


def run_in_pane(window, task, progress_file):
    script_file = tempfile.mktemp()
    script = """echo 'plex>' Running task: {0} 1>&2
echo 'plex>' {3} 1>&2
function plex_cleanup {{
    RC=$?
    echo $TMUX_PANE $RC >> {1}
    exit $RC
}}
rm {2}
trap plex_cleanup SIGINT SIGQUIT SIGTERM EXIT
{3}
""".format(task.name, progress_file, script_file, task.command)

    with open(script_file, 'w') as f:
        f.write(script)

    pane = window.split_window()
    window.select_layout('tiled')
    pane.send_keys('sh {} && exit'.format(script_file))
    return pane.get('pane_id')


class Task(object):
    def __init__(self, name, command, depends=None, **kwargs):
        self.name = name
        self.depends = set(depends) if depends else set()
        self.command = command
        self.started = False
        self.completed = False
        self.start_time = None
        self.pane_id = None
        self.end_time = None
        self.return_code = None
        self.spinner = cycle('/-\\|')

        for k, v in kwargs.iteritems():
            setattr(self, k, v)

    def start(self, window, progress_file):
        self.pane_id = run_in_pane(window, self, progress_file)
        self.started = True
        self.start_time = time.time()

    def complete(self, return_code):
        self.completed = True
        self.end_time = time.time()
        self.return_code = int(return_code)

    def __repr__(self):
        if self.completed:
            if self.return_code == 0:
                status = 'SUCCEEDED'
            else:
                status = 'FAILED'
        elif self.started:
            status = 'STARTED'
        else:
            status = 'PENDING'
        return 'Task({}, {})'.format(self.name, status)


def execute(window, flow, progress_file):
    incomplete = {task.name: task for task in flow if not task.completed}
    failed = {task.name for task in flow if task.return_code != 0}
    runnable = [task for task in incomplete.itervalues()
                if not task.started and not task.depends & (incomplete.viewkeys() | failed)]
    running = [task for task in incomplete.itervalues() if task.started]
    if not runnable and not running:
        return not failed

    for task in runnable:
        try:
            task.start(window, progress_file)
        except tmuxp.exc.TmuxpException:
            continue

    while True:
        try:
            pane_id, return_code = queue.get(timeout=0.3)
            for task in incomplete.itervalues():
                if task.pane_id == pane_id:
                    task.complete(return_code)
                    return execute(window, flow, progress_file)
        except Queue.Empty:
            pass
        finally:
            print_rows(report(flow))


def print_rows(rows):
    click.clear()
    click.echo(tabulate.tabulate(rows, headers=['', 'task', 'duration', 'cumulative']))


def report(flow):
    min_start_time = min(task.start_time for task in flow if task.start_time)
    for task in flow:
        if not task.started:
            check = ' '
            delta = (click.style('PENDING', fg='blue'), '')
        elif task.completed:
            check = V if task.return_code == 0 else X
            delta = (fmt_time(task.end_time - task.start_time), fmt_time(task.end_time - min_start_time))
        else:
            check = next(task.spinner)
            delta = (fmt_time(time.time() - task.start_time), fmt_time(time.time() - min_start_time))
        yield (check, task.name) + delta


def tail_f(filename):
    with open(filename) as f:
        accumulated = []
        while True:
            for line in iter(f.readline, ''):
                accumulated.append(line)
                if line.endswith('\n'):
                    yield ''.join(accumulated)
                    accumulated = []
            time.sleep(0.3)


def tail_f_loop(progress_file):
    for line in tail_f(progress_file):
        pane_id, status = line.strip().split()
        queue.put((pane_id, status))


def run(flow, env):
    window = get_window(env)

    progress_file = tempfile.mktemp()
    with open(progress_file, 'w'):
        pass

    t = threading.Thread(target=tail_f_loop, args=(progress_file,))
    t.daemon = True
    t.start()

    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)
    t0 = time.time()
    if execute(window, flow, progress_file):
        print_rows(chain(report(flow), [(V, click.style('SUCCESS', bg='green', fg='black'),
                                         fmt_time(time.time() - t0), '')]))
        return True
    else:
        print_rows(chain(report(flow), [(X, click.style('FAILED', bg='red'), fmt_time(time.time() - t0), '')]))
        return False


def reset_task(dct):
    del dct['started']
    del dct['completed']
    del dct['return_code']
    del dct['start_time']
    del dct['end_time']
    return dct


def task_constructor(dct):
    if dct.get('started'):
        if dct.get('completed'):
            if dct.get('return_code', 0) != 0:
                reset_task(dct)
        else:
            reset_task(dct)
    return Task(**dct)


def task_representer(dumper, task):
    dct = {
        'name': task.name,
        'command': task.command,
        'depends': list(task.depends),
        'started': task.started,
        'completed': task.completed,
        'start_time': task.start_time,
        'end_time': task.end_time,
        'return_code': task.return_code
    }
    return dumper.represent_dict(dct)


yaml.add_representer(Task, task_representer)


def load(path):
    with open(path) as f:
        manifest = yaml.safe_load(f)
        manifest['flow'] = map(task_constructor, manifest['flow'])
        return manifest


@click.command()
@click.option('--save/--no-save', default=True)
@click.option('--save-dir', default='/tmp', type=click.Path(file_okay=False, writable=True))
@click.argument('manifest-file', type=click.Path(dir_okay=False))
def main(save, save_dir, manifest_file):
    save_file = os.path.join(save_dir, manifest_file)
    try:
        manifest = load(save_file)
    except IOError:
        manifest = load(manifest_file)

    success = run(manifest['flow'], manifest['env'])
    if save:
        with open(save_file, 'w') as f:
            yaml.dump(manifest, f)
    sys.exit(not success)


if __name__ == '__main__':
    main()
