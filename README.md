# plex - tmux task runner

*plex* runs a list of dependant tasks supplied by a yaml file.

#### Basically, this:
```yaml
env:
  URL: 'https://raw.githubusercontent.com/bergundy/ec2grep/master/README.md'
  REPORT: /tmp/report.txt
  RAW: /tmp/raw.md
  TARGET: /tmp/target.md

flow:
- name: Clear report
  command: cat /dev/null > $REPORT

- name: Download HTML
  command: curl -o $RAW $URL

- name: Prepare words
  command: "cat $RAW | tr ' ' '\\n' > $TARGET"
  depends:
  - Download HTML

- name: Count words
  command: sleep 1; echo 'Word count:' $(wc -l $TARGET) >> $REPORT
  depends:
  - Prepare words
  - Clear report

- name: Count distinct words
  command: sleep 1; echo 'Distinct words:' $(sort -u $TARGET | wc -l) >> $REPORT
  depends:
  - Prepare words
  - Clear report

- name: Display report
  command: vim $REPORT
  depends:
  - Count words
  - Count distinct words
```

#### Turns into this:

[![Click to watch](https://raw.githubusercontent.com/bergundy/plex/master/plex-screenshot.png)](http://www.youtube.com/watch?v=ujhqZIRUh48 "Click to watch")


### Installation
```sh
pip install git+https://github.com/bergundy/plex
```

### Running
```sh
plex [--restart] [--no-save] [--save-file <SAVE_FILE>] <MANIFEST_FILE_YML>
```
* `--restart` causes plex to ignore save file state
* `--save-file` choose custom location for save file, defaults to `<OS_TEMP_DIR>/.plex-save-<MANIFEST_FILE_YML>`


### Features
* Track task status
* Task dependency resolution
* Save / resume
* Shell automatically closes if command succeeded
* Shell lingers to enable manual intervention if command failed
* Retry failed commands (press key `<UP>` in failed shell to re-execute)
