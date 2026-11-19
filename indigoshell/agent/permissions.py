"""What the agent may do without asking.

The call runs Claude Code in auto mode: a classifier approves routine
tool calls and blocks risky ones (telling the agent why, so it finds
another way). It decides everything, deletes and kills included, except
the few `ASK_FIRST` rules below, which always come to the call as a
greenlight / scrub question (no "always": each one is worth a look).
The workspace's .claude/settings.json can add rules of its own."""


# Tool calls that always come to the overlay for a yes or no, whatever the
# permission mode decides. Claude Code's `ask` rules: a Bash rule matches
# a command by its prefix, and compound commands are checked part by part.
#
# Kept short on purpose, so each question is rare enough to read
# (2026-11-19: every question in the first days was an `rm` or `kill`
# tidying /tmp, and the user stopped reading them). What is left is
# root: this machine's sudo is NOPASSWD, so without the rule a root
# command would run on the classifier's word alone. (git push was here
# too; the user dropped it: nothing this agent touches is production.)
ASK_FIRST = (
    "Bash(sudo *)", "Bash(doas *)", "Bash(pkexec *)", "Bash(su *)",
)
