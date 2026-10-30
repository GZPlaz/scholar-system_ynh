### First steps

1. Open the app and sign in as the administrator you chose during installation (YunoHost portal or your YunoHost username and password).
2. In **Administration → Settings**, check the institution name, time zone and mastery thresholds.
3. Add instructors in **Administration → Users** (YunoHost accounts or local accounts). Unknown YunoHost users can't sign in unless you allow them to become instructors in **Administration → Settings**.
4. Add students. Students have **no account and no password**: a student is a name and a student ID. Instructors add them on a course's **Roster** (one by one or from a CSV with `student_id` and `name`), administrators via **Administration → Import users**, and the RosarioSIS plugin syncs them automatically.

### Who can reach the app

- The **staff site** (everything except `/test`) follows the main permission. With **visitors** (default), local staff accounts can sign in; with **all_users** (or a group) only YunoHost users reach it. YunoHost users already signed into the portal are logged in automatically.
- The **test portal** (`https://__DOMAIN____PATH__/test/`) has its own permission, always open to visitors, because students have no YunoHost account. It never accepts a staff session, and students can never sign in to the staff site.

### Test portal and test security

1. A student opens `/test/`, types the assessment's **test code** (on the Proctor tab; regenerate it any time) and their **student ID**.
2. Scholar shows the first name and last initial for that ID; the student confirms it's them. If the assessment asks for a **PIN**, they type their personal PIN too. Give out PINs from the course roster (**Test-portal PINs**): they are stored hashed and shown once, on printable slips.
3. If the assessment requires **proctor admission**, the student waits until a proctor lets them in on the **Proctor** tab.

Each assessment's **Settings → Test-taking security** controls: the identity check (name or name + PIN), proctor admission, one device per attempt, full screen, copy/paste/print blocking, the name watermark, how many times a student may leave the test before it pauses, allowed networks (IP ranges, e.g. the campus range) and a late-start limit, alongside the time limit, availability window and attempts.

What the server enforces regardless of the browser: the time limit and closing time, the attempt limit, network allowlist, late start, one device per attempt, PIN throttling (5 wrong PINs per student per 15 minutes) and code/ID guessing limits (60 per network per 15 minutes; classrooms share an address, so this never bans anyone at the firewall). Portal sessions are random tokens stored hashed, rotated after the identity check, bound to the browser, in an HttpOnly SameSite=Strict cookie limited to `/test`, with a strict Content Security Policy.

What the browser reports (full screen, leaving the page, copy/paste attempts) is recorded and counted on the server; past the limit the server pauses the attempt. A browser can't prove that nobody uses a second device, so for high-stakes exams combine these settings with a proctor in the room, proctor admission and PINs.

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

Records the plugin creates carry an external ID such as `rosario:student:42`; a student's RosarioSIS student ID becomes their Scholar student ID, which they type in the test portal. The plugin's account type only concerns teachers: “YunoHost accounts” if they sign in with YunoHost, otherwise local accounts with the temporary passwords the plugin shows.

### Command-line tools

All commands run as the app user from the install directory (`__INSTALL_DIR__`):

```bash
cd __INSTALL_DIR__/app
sudo -u __APP__ env SCHOLAR_CONFIG=__INSTALL_DIR__/scholar.env __INSTALL_DIR__/venv/bin/python manage.py --help
```

- `create-local-admin USERNAME` — emergency local administrator with a temporary password (useful if LDAP/SSO is unavailable).
- `seed-demo` — loads a demo course with standards, questions and simulated results (`demo.instructor` / `instructor123`; students `MA2026001`…`MA2026008` take the demo test in the test portal with the code shown on its Proctor tab). Don't use it on a production instance, or delete the demo users and course afterwards.
- `upgrade-db` — applies database migrations (run automatically on upgrade).
- `create-api-token NAME` — creates an integration API token and prints it once (same as **Administration → Integrations**).

### Logs, backups

- Application log: `/var/log/__APP__/__APP__.log`.
- Backups made with YunoHost include the PostgreSQL database and the configuration.
