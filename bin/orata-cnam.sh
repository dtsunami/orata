#!/bin/bash
#
# orata-cnam.sh -- manage the spoken-name book and the robocall whitelist.
# Both live in astdb, Asterisk's built-in key/value store.
#

set -u
AST="asterisk -rx"

usage() {
    cat <<'EOF'
usage:
  orata-cnam.sh add <number> <name>   add/update a spoken name
  orata-cnam.sh del <number>          remove a spoken name
  orata-cnam.sh list                  show the name book

  orata-cnam.sh allow-list            show robocall-gate whitelist
  orata-cnam.sh allow-add <number>    whitelist a number
  orata-cnam.sh allow-del <number>    revoke (they'll get the gate again)

  orata-cnam.sh block-list            show blocklist
  orata-cnam.sh block-add <number>    hang up on this number, always
  orata-cnam.sh block-del <number>    unblock

  orata-cnam.sh sensor-list           show number -> Alexa sensor map
  orata-cnam.sh sensor-add <number> <endpointId>
  orata-cnam.sh sensor-del <number>

endpointId must match a sensor your Smart Home skill reports in its
Discovery response, e.g. orata-mom. Only used when ORATA_ALEXA_MODE
is sensor or both.

numbers are as voip.ms presents them, usually 11-digit: 15551234567
EOF
    exit 1
}

case "${1:-}" in
    add)
        [ $# -ge 3 ] || usage
        $AST "database put cnam $2 \"$3\"" ;;
    del)
        [ $# -ge 2 ] || usage
        $AST "database del cnam $2" ;;
    list)
        $AST "database show cnam" ;;
    allow-list)
        $AST "database show allow" ;;
    allow-add)
        [ $# -ge 2 ] || usage
        $AST "database put allow $2 1" ;;
    allow-del)
        [ $# -ge 2 ] || usage
        $AST "database del allow $2" ;;
    sensor-list)
        $AST "database show alexasensor" ;;
    sensor-add)
        [ $# -ge 3 ] || usage
        $AST "database put alexasensor $2 \"$3\"" ;;
    sensor-del)
        [ $# -ge 2 ] || usage
        $AST "database del alexasensor $2" ;;
    block-list)
        $AST "database show block" ;;
    block-add)
        [ $# -ge 2 ] || usage
        $AST "database put block $2 1" ;;
    block-del)
        [ $# -ge 2 ] || usage
        $AST "database del block $2" ;;
    *)
        usage ;;
esac