Scholar System is installed at https://__DOMAIN____PATH__ : the student test portal is the front page, and instructors and administrators sign in at https://__DOMAIN____PATH__/staff .

Sign in at /staff with the YunoHost account **__ADMIN__** — it is the first Scholar administrator. Then go to **Administration → Settings** to check the time zone and mastery thresholds, and add instructors under **Administration → Users**. Students need no account: add them (name and student ID) to a course roster, and they take tests at https://__DOMAIN____PATH__/ with the test code shown on each assessment's Proctor tab.

Want to explore with sample data first? Run `manage.py seed-demo` (see the admin documentation).
