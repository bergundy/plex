try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

setup(
    name='plex',
    description='Run parallel tasks in Tmux panes',
    author='Roey Berman',
    author_email='roey.berman@gmail.com',
    py_modules=['plex'],
    version='0.2',
    keywords=['tmux', 'cli', 'task', 'runner'],
    install_requires=[
        'tmuxp==0.11.0',
        'tabulate',
        'click',
        'pyyaml'
    ],
    entry_points={
        'console_scripts': [
            'plex = plex:main'
        ]
    }
)
