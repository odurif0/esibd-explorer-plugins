# Sync Plugin

Copies Explorer's `conf` and `data` folders to a network folder, by default `\\hive\esi`, every
hour and after each new scan file. The copy goes one way: the local folders are only read, and
nothing is ever deleted on the network folder.

Enable the `Sync` plugin in Explorer's Plugin Manager. When installing or updating,
keep the whole `sync/` directory, including `_runtime/`: the entrypoint loads `_runtime/_sync.py` (what
to copy and when) and `_runtime/_windows.py` (the network account) privately. The plugin imports
no other plugin and drives no device. Windows only: the copy is made by robocopy, part of
Windows. MIT license (see `LICENSE`).

## Panel

- **Destination**: the network folder. Each PC copies into `<destination>\<PC name>\conf` and
  `<destination>\<PC name>\data`, so several PCs never mix their files.
- **Account** and **Connect…**: the account of the network folder (`DOMAIN\user` or `user`) and
  its password. The plugin opens the folder with them at once and, with **Keep it on this PC**,
  gives them to the Windows Credential Manager, which uses them for every later copy, as
  `cmdkey /add:hive` does. The password is never written in Explorer's files (`conf` is itself
  copied). Anyone using the same Windows account on this PC then reaches the folder with it:
  prefer an account limited to that folder. Windows keeps one account per server: if another one
  is already connected, disconnect it (`net use \\hive\esi /delete`) or log off first.
- **Automatic**: a copy every hour, and one after each new scan file (`.h5` in the data folder,
  outside `logs`), once it is 5 min old.
- **Last copy**: when, how many files were copied, how many were left for the next copy, or why
  it failed. After a failure the next automatic attempt waits 10 min.
- **Copy now**: a copy at once.

## What is copied

robocopy copies new and changed files only (`/E /XO /FFT /Z /R:2 /W:10`), in its own process,
from a background thread: Explorer never waits for the network, and an interrupted copy resumes.

- **Nothing is deleted**: no `/MIR` and no `/PURGE`; a file removed on the PC stays on the
  network folder. A file newer on the network folder is kept (`/XO`).
- **Files being written are left for the next copy**: a file modified less than 5 min ago is
  skipped (Explorer saves its device histories every hour), as well as `__pycache__`, `*.tmp` and
  `*.lock`.
- **Except the logs** (`*.log`, `*.jsonl`: `esibd explorer.log`, `data/logs`): written all the
  time, they would never be copied while Explorer runs, and they are what explains a stopped scan.
  Text that only grows, they are copied at every copy when they changed, by the plugin itself,
  which reads a file another program holds open (robocopy refuses it). A log shorter than its copy
  started again: the copy on the network folder is kept first as `<name>.until-<date>`.
- The settings of the panel (destination, account name, last copy) are kept in
  `conf/Sync.json`, without any password.
- Each copy adds a line to `<data>/logs/sync/sync.log`: time, result, files copied and left, and
  the last lines of robocopy when it failed.
