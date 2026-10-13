# Sourced by the shell tools (backlog 019): load ~/.config/epgstation-kodi/local.env (path: $LOCAL_ENV) the way
# tools/local_env.py does: `KEY=value` or `KEY = value`, optional `export `, blank lines and # comments skipped,
# surrounding whitespace (and a CR from Windows line ends) stripped. A variable set in the environment wins.
_local_env=${LOCAL_ENV:-$HOME/.config/epgstation-kodi/local.env}
_trim() { local v=$1; v=${v//$'\r'/}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}; printf '%s' "$v"; }
if [ -f "$_local_env" ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line=$(_trim "$_line")
        case $_line in ''|'#'*) continue ;; esac
        case $_line in *=*) ;; *) continue ;; esac
        _key=$(_trim "${_line%%=*}")
        _key=$(_trim "${_key#export }")
        _value=$(_trim "${_line#*=}")
        case $_key in ''|*[!A-Za-z0-9_]*|[0-9]*) continue ;; esac
        [ -n "${!_key+x}" ] || export "$_key=$_value"
    done < "$_local_env"
fi
unset -f _trim
unset _local_env _line _key _value
