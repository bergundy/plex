import Queue
import logging
import sys
import threading

import os
import time
from itertools import cycle, chain, count

import click
import tabulate
import tmuxp
import tmuxp.exc
import yaml
import tempfile


queue = Queue.Queue()
select_keys = (lambda dct, *keys: {k: dct[k] for k in keys})
parenthesize = '({})'.format
V = click.style(u'\u2713', fg='green')
X = click.style('X', fg='red')


def fmt_time(s):
    hours, remainder = divmod(s, 3600)
    minutes, seconds = divmod(remainder, 60)
    return '{:02.0f}:{:02.0f}:{:02.0f}'.format(hours, minutes, seconds)


def get_window(env):
    s = tmuxp.Server()
    for k, v in env.iteritems():
        s.set_environment(k, v)
    session = s.getById('$' + os.environ['TMUX'].split(',')[-1])
    if session is None:
        raise ValueError('session can not be None')
    # noinspection PyUnresolvedReferences
    return session.attached_window()


def run_in_pane(window, task, progress_file):
    script_file = tempfile.mktemp()
    script = """echo 'plex>' Starting: {name!r} 1>&2
echo 'plex>' {command!r} 1>&2
function plex_cleanup {{
    RC=$?
    echo $RC {name!r} >> {progress_file}
    exit $RC
}}
trap plex_cleanup SIGINT SIGQUIT SIGTERM EXIT
{command}
""".format(name=task.name, command=task.command, progress_file=progress_file, script_file=script_file)

    with open(script_file, 'w') as f:
        f.write(script)

    panes = window.list_panes()
    free_panes = get_dead_panes(panes)
    if free_panes:
        pane = free_panes[0]
        pane.cmd('respawn-pane', 'sh {} && exit'.format(script_file))
    else:
        if len(panes) >= 4:
            raise RuntimeError("Out of panes")
        else:
            pane = window.split_window()
            window.select_layout('tiled')
            pane.send_keys('sh {} && exit'.format(script_file))

    return pane.get('pane_id')


def get_dead_panes(panes):
    free_panes = [pane for pane in panes if pane.get('pane_dead') == '1']
    return free_panes


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

    def start(self, pane_id):
        self.pane_id = pane_id
        self.started = True
        self.start_time = time.time()

    def complete(self, return_code):
        self.pane_id = None
        self.completed = True
        self.end_time = time.time()
        self.return_code = int(return_code)

    @property
    def status(self):
        if self.completed:
            if self.return_code == 0:
                return 'SUCCEEDED'
            else:
                return 'FAILED'
        elif self.started:
            return 'STARTED'
        else:
            return 'PENDING'

    def __repr__(self):
        return 'Task({}, {})'.format(self.name, self.status)


def traverse(window, flow, progress_file):
    last_done = 0

    for i in count():
        possibly_done = False

        runnable, running, incomplete, failed = get_run_status(flow)
        if not runnable and not running:
            panes = window.list_panes()
            dead_panes = get_dead_panes(panes)
            if len(panes) - 1 == len(dead_panes):
                possibly_done = True

        for task in runnable:
            try:
                pane_id = run_in_pane(window, task, progress_file)
            except (tmuxp.exc.TmuxpException, RuntimeError):
                continue
            else:
                task.start(pane_id)

        try:
            name, return_code = queue.get(timeout=0.1)
            last_done = i
            for task in incomplete + failed:
                if task.name == name:
                    task.complete(return_code)
                    break
        except Queue.Empty:
            if possibly_done:
                return not failed
            if (i - last_done) % 20 == 19:
                kill_dead_panes(window)

        print_rows(report(flow))


def get_run_status(flow):
    names = (lambda tasks: {t.name for t in tasks})
    incomplete = [task for task in flow if not task.completed]
    failed = [task for task in flow if task.return_code != 0]
    running = [task for task in incomplete if task.started]
    runnable = [task for task in incomplete
                if not task.started and not task.depends & (names(incomplete) | names(failed))]
    return runnable, running, incomplete, failed


def print_rows(rows):
    click.clear()
    click.echo(tabulate.tabulate(rows, headers=['', 'task', 'duration', 'cumulative']))


def report(flow):
    min_start_time = min(task.start_time for task in flow if task.start_time)
    for task in flow:
        if not task.started:
            check = ' '
            delta = (click.style('PENDING', fg='blue'), '')
            name = task.name
        elif task.completed:
            check = V if task.return_code == 0 else X
            delta = (fmt_time(task.end_time - task.start_time),
                     parenthesize(fmt_time(task.end_time - min_start_time)))
            name = task.name
        else:
            check = next(task.spinner)
            delta = (fmt_time(time.time() - task.start_time),
                     parenthesize(fmt_time(time.time() - min_start_time)))
            name = click.style(task.name, fg='cyan')
        yield (check, name) + delta


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
        return_code, name = line.strip().split(' ', 1)
        queue.put((name, return_code))


def print_conclusion(flow, success, start_time):
    if success:
        conclusion = (V, click.style('SUCCESS', bg='green', fg='black'))
    else:
        conclusion = (X, click.style('FAILED', bg='red'))

    print_rows(chain(report(flow), [conclusion + (fmt_time(time.time() - start_time), '')]))


def run(flow, env):
    window = get_window(env)
    window.set_window_option('remain-on-exit', 'on')

    progress_file = tempfile.mktemp()
    with open(progress_file, 'w'):
        pass

    t = threading.Thread(target=tail_f_loop, args=(progress_file,))
    t.daemon = True
    t.start()

    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)
    t0 = time.time()
    # noinspection PyBroadException
    try:
        success = traverse(window, flow, progress_file)
        print_conclusion(flow, success, t0)
    except BaseException:
        success = False
        print_conclusion(flow, success, t0)
        logging.exception('Failed to finish running flow')

    window.set_window_option('remain-on-exit', 'off')
    kill_dead_panes(window)
    return success


def kill_dead_panes(window):
    dead_panes = [pane for pane in window.list_panes() if pane.get('pane_dead') == '1']
    for pane in dead_panes:
        pane.cmd('kill-pane')


def task_repr(dumper, task):
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


yaml.add_representer(Task, task_repr)


def should_reset(dct):
    if dct.get('started'):
        if dct.get('completed'):
            return dct.get('return_code', 0) != 0
        else:
            return True
    else:
        return False

reset_task = (lambda dct: select_keys(dct, 'name', 'command', 'depends'))


def load(path):
    with open(path) as f:
        manifest = yaml.safe_load(f)
        flow = [Task(**(reset_task(dct) if should_reset(dct) else dct)) for dct in manifest['flow']]
        return {'flow': flow, 'env': dict(manifest['env'])}


@click.command()
@click.option('--restart/--no-restart', default=False)
@click.option('--save/--no-save', default=True)
@click.option('--save-file', type=click.Path(file_okay=False, writable=True))
@click.argument('manifest-file', type=click.Path(dir_okay=False))
def main(restart, save, save_file, manifest_file):
    save_file = save_file or os.path.join(tempfile.gettempdir(), '.plex-save-' + manifest_file)
    if not restart:
        try:
            manifest = load(save_file)
        except IOError:
            manifest = load(manifest_file)
    else:
        manifest = load(manifest_file)

    success = run(manifest['flow'], manifest['env'])
    if save:
        with open(save_file, 'w') as f:
            yaml.dump(manifest, f)
    sys.exit(not success)


if __name__ == '__main__':
    main()
