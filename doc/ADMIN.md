### First steps

1. Open the app and sign in as the administrator you chose during installation (YunoHost portal or your YunoHost username and password).
2. In **Administration → Settings**, check the institution name, time zone and mastery thresholds.
3. Add instructors: any YunoHost user who signs in gets a Scholar profile (as a *student* by default — change the default role in Settings). Promote them in **Administration → Users**.
4. Add students either as YunoHost users or as local Scholar accounts via **Administration → Import users** (CSV). Generated temporary passwords are downloaded as a CSV; students must change them at first sign-in.

### Who can reach the app

- With the main permission set to **visitors** (default), the sign-in page is public so students with local Scholar accounts can use it. YunoHost users already signed into the portal are logged in automatically.
- With the permission set to **all_users** (or a group), only YunoHost users can reach the app at all — local accounts are then useless.

### Command-line tools

All commands run as the app user from the install directory (`__INSTALL_DIR__`):

```bash
cd __INSTALL_DIR__/app
sudo -u __APP__ env SCHOLAR_CONFIG=__INSTALL_DIR__/scholar.env __INSTALL_DIR__/venv/bin/python manage.py --help
```

- `create-local-admin USERNAME` — emergency local administrator with a temporary password (useful if LDAP/SSO is unavailable).
- `seed-demo` — loads a demo course with standards, questions and simulated results (`demo.instructor` / `instructor123`, `demo.student1…8` / `student123`). Don't use it on a production instance, or delete the demo users and course afterwards.
- `upgrade-db` — applies database migrations (run automatically on upgrade).

### Logs, backups

- Application log: `/var/log/__APP__/__APP__.log`.
- Backups made with YunoHost include the PostgreSQL database and the configuration.
