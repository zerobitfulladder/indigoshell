"""What the agent may do without asking.

The call runs Claude Code in auto mode: a classifier approves routine
tool calls and blocks risky ones (telling the agent why, so it finds
another way). `ASK_FIRST` are Claude Code `ask` rules on top of that —
these always come to the call as a Y / A / N question. Edit the list to
taste; the workspace's .claude/settings.json can add rules of its own."""


# Tool calls that always come to the overlay for a yes or no, whatever the
# permission mode decides. Claude Code's `ask` rules: a Bash rule matches
# a command by its prefix, and compound commands are checked part by part.
# Auto mode already blocks the worst of these on its own (and always
# refuses to delete critical system paths); this list is for the ones it
# might allow but you would want to see first.
ASK_FIRST = (
    "Bash(rm *)", "Bash(rmdir *)", "Bash(shred *)", "Bash(unlink *)",
    "Bash(dd *)", "Bash(mkfs *)", "Bash(mkfs.*)", "Bash(wipefs *)",
    "Bash(fdisk *)", "Bash(sfdisk *)", "Bash(parted *)", "Bash(cryptsetup *)",
    "Bash(sudo *)", "Bash(doas *)", "Bash(pkexec *)", "Bash(su *)",
    "Bash(kill *)", "Bash(pkill *)", "Bash(killall *)",
    "Bash(reboot *)", "Bash(reboot)", "Bash(shutdown *)", "Bash(poweroff)",
    "Bash(systemctl stop *)", "Bash(systemctl disable *)",
    "Bash(systemctl mask *)", "Bash(systemctl --user stop *)",
    "Bash(chmod -R *)", "Bash(chown -R *)",
    "Bash(git push *)", "Bash(git reset --hard *)", "Bash(git clean *)",
)
