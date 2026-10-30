### First steps

1. Open the app and sign in as the administrator you chose during installation (YunoHost portal or your YunoHost username and password).
2. In **Administration → Settings**, check the institution name, time zone and mastery thresholds.
3. Add instructors: any YunoHost user who signs in gets a Scholar profile (as a *student* by default — change the default role in Settings). Promote them in **Administration → Users**.
4. Add students either as YunoHost users or as local Scholar accounts via **Administration → Import users** (CSV). Generated temporary passwords are downloaded as a CSV; students must change them at first sign-in.

### Who can reach the app

- With the main permission set to **visitors** (default), the sign-in page is public so students with local Scholar accounts can use it. YunoHost users already signed into the portal are logged in automatically.
- With the permission set to **all_users** (or a group), only YunoHost users can reach the app at all — local accounts are then useless.

### Scanning answer sheets

Scanning uses OpenCV, which installs from prebuilt packages on x86_64 and 64-bit ARM servers. On 32-bit ARM (armhf) it is skipped: the app works normally and paper answers are typed in instead.

Tips for instructors:

- Print answer sheets at 100% (actual size) on A4 or US Letter. Printing several times is fine; each student keeps the same sheet code.
- Photos can be taken at an angle and either way up, but all four black corner squares must be in the picture.
- A photo is stored after it is straightened (about 200 KB), so you can always see what a recorded answer was based on.
- Uploads are limited to 25 MB per photo; the scan page shrinks photos in the browser before sending them.

### Connecting RosarioSIS

Scholar can be linked to a RosarioSIS student information system with the [RosarioSIS Scholar plugin](https://github.com/GZPlaz/RosarioSIS-scholar-plugin). Once linked, RosarioSIS course periods become Scholar courses (teacher and students included), and Scholar test results are written into the RosarioSIS gradebook.

1. In Scholar, open **Administration → Integrations** and create a token named “RosarioSIS”. Copy it: it is shown only once.
2. In RosarioSIS, install and activate the plugin, then enter Scholar's address (`https://__DOMAIN____PATH__`) and the token in its configuration.

The plugin calls `https://__DOMAIN____PATH__/api/v1/…`. The `api` permission (visitors, no portal redirect) lets these server-to-server requests through the YunoHost portal; every request still needs a valid token. Revoke a token in **Administration → Integrations** to cut access immediately.

Accounts the plugin creates carry an external ID such as `rosario:student:42`. If RosarioSIS students also have YunoHost accounts with the same username, choose “YunoHost accounts” in the plugin; otherwise choose local accounts and give students the temporary passwords the plugin shows.

### Command-line tools

All commands run as the app user from the install directory (`__INSTALL_DIR__`):

```bash
cd __INSTALL_DIR__/app
sudo -u __APP__ env SCHOLAR_CONFIG=__INSTALL_DIR__/scholar.env __INSTALL_DIR__/venv/bin/python manage.py --help
```

- `create-local-admin USERNAME` — emergency local administrator with a temporary password (useful if LDAP/SSO is unavailable).
- `seed-demo` — loads a demo course with standards, questions and simulated results (`demo.instructor` / `instructor123`, `demo.student1…8` / `student123`). Don't use it on a production instance, or delete the demo users and course afterwards.
- `upgrade-db` — applies database migrations (run automatically on upgrade).
- `create-api-token NAME` — creates an integration API token and prints it once (same as **Administration → Integrations**).

### Logs, backups

- Application log: `/var/log/__APP__/__APP__.log`.
- Backups made with YunoHost include the PostgreSQL database and the configuration.
