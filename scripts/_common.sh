#!/bin/bash

#=================================================
# COMMON VARIABLES AND CUSTOM HELPERS
#=================================================

# The application code ships inside this package (sources/) rather than as an
# upstream release tarball, so it is copied in place instead of using ynh_setup_source.
_scholar_setup_source() {
    if [ -d "$install_dir/app" ]; then
        ynh_safe_rm "$install_dir/app"
    fi
    mkdir -p "$install_dir/app"
    cp -a ../sources/. "$install_dir/app/"
    find "$install_dir/app" -name '__pycache__' -type d -prune -exec rm -rf {} +
    rm -f "$install_dir/app/"*.sqlite3
}

# (Re)create the Python virtualenv. Always rebuilt so a Debian/Python upgrade never
# leaves a venv pointing at a removed interpreter.
_scholar_build_venv() {
    ynh_safe_rm "$install_dir/venv"
    python3 -m venv "$install_dir/venv"
    ynh_hide_warnings "$install_dir/venv/bin/pip" install --no-cache-dir --upgrade pip wheel
    ynh_hide_warnings "$install_dir/venv/bin/pip" install --no-cache-dir -r "$install_dir/app/requirements.txt"
}

_scholar_set_permissions() {
    chown -R "$app:www-data" "$install_dir"
    chmod -R u=rwX,g=rX,o= "$install_dir"
    # secrets: readable by the app only
    chown "$app:$app" "$install_dir/scholar.env"
    chmod 400 "$install_dir/scholar.env"

    mkdir -p "/var/log/$app"
    touch "/var/log/$app/$app.log"
    chown -R "$app:$app" "/var/log/$app"
    chmod 750 "/var/log/$app"
    chmod 640 "/var/log/$app/$app.log"
}

# Run a Scholar management command (see sources/manage.py) as the app user.
_scholar_manage() {
    (
        cd "$install_dir/app"
        ynh_exec_as_app env SCHOLAR_CONFIG="$install_dir/scholar.env" \
            "$install_dir/venv/bin/python" "$install_dir/app/manage.py" "$@"
    )
}

_scholar_add_configs() {
    ynh_config_add --template="scholar.env" --destination="$install_dir/scholar.env"
    ynh_config_add --template="gunicorn.conf.py" --destination="$install_dir/gunicorn.conf.py"
}

_scholar_failregex="Failed login attempt for .* from <HOST>$"
