#!/bin/sh
# Shared helper: the tests/ line budget in the index and in HEAD, and the staged line count.
#
# Agents add a test for every change they make and rarely remove one, so the suite grows
# faster than the code it guards. The budget makes that growth a decision: a commit that
# touches tests/ must leave the budget equal to the non-blank lines under tests/, and only
# a commit of its own, carrying the user's instruction, may raise it.

BUDGET_FILE=tests/line-budget

budget_staged() {
    git show ":$BUDGET_FILE" 2>/dev/null | tr -dc '0-9'
}

budget_head() {
    git show "HEAD:$BUDGET_FILE" 2>/dev/null | tr -dc '0-9'
}

budget_count() {
    git grep --cached -h -c -E '[^[:space:]]' -- 'tests/*.py' | awk '{s += $1} END {print s + 0}'
}

# True when the staged budget is above the one in HEAD.
budget_raised() {
    staged=$(budget_staged)
    head=$(budget_head)
    [ -n "$staged" ] && [ -n "$head" ] && [ "$staged" -gt "$head" ]
}
