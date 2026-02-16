# First Login and Admin Password

This guide explains how to log in for the first time and how to change the admin password.

---

## First Login

On first run, Stargate creates a default admin user:

| Field    | Value    |
|----------|----------|
| Username | `admin`  |
| Password | `admin123` |

1. Open Stargate in your browser (e.g. http://localhost:8080).
2. Enter **admin** as username and **admin123** as password.
3. Click **Login**.

**Important:** Change this password immediately after first login.

---

## Changing the Password

### Via the UI (recommended)

1. Log in as admin.
2. Click your username or avatar in the top-right corner.
3. Select **Profile**.
4. In **Account settings**, find the **Change Password** section.
5. Enter:
   - **Current password** — your current password (e.g. `admin123`)
   - **New password** — at least 6 characters
   - **Confirm new password**
6. Click **Change Password**.

---

## Resetting a Lost Password

If you have lost the admin password and cannot log in:

### Option 1: Run the reset script (local install)

From the project root:

```bash
cd backend
python update_admin_password.py
```

If your data directory is elsewhere, set `DATA_DIR`:

```bash
DATA_DIR=/path/to/data python update_admin_password.py
```

This resets the admin password to `admin123`. Log in and change it via Profile.

### Option 2: Docker

If running in Docker, execute the script inside the backend container:

```bash
docker exec -it stargate-backend python -c "
import sys
sys.path.insert(0, '/app')
from user_service import UserService
from pathlib import Path
us = UserService(Path('/app/data'))
admin = us.get_user_by_username('admin')
if admin:
    us.set_password(admin.id, 'admin123')
    print('Password reset to admin123')
else:
    print('Admin user not found')
"
```

Then log in with `admin` / `admin123` and change the password.

---

## Security Notes

- The default password `admin123` is intended only for initial setup.
- Change it before exposing Stargate to a network or the internet.
- Use a strong password (at least 6 characters; longer and mixed characters are recommended).
