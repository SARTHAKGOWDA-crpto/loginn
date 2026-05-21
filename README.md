# NEXUS AUTH — Flask + MySQL Authentication System

A professional, full-featured authentication system built with Flask and MySQL,
featuring a stunning dark luxury UI designed from scratch.

---

## Features

| Feature              | Details                                              |
|----------------------|------------------------------------------------------|
| Login / Logout       | Email or username login, remember-me, session mgmt  |
| Registration         | Full validation, password strength meter             |
| Forgot Password      | Email reset link, 30-min expiry, single-use tokens   |
| Account Locking      | Auto-lock after 5 failed attempts                    |
| Audit Log            | Every login attempt logged with IP + outcome         |
| Password Hashing     | bcrypt via Werkzeug                                  |
| Role-based Access    | user / admin roles                                   |
| Profile Management   | Edit name, phone, change password                    |
| Password Strength API| Real-time AJAX strength checker                     |
| Dark Luxury UI       | Custom CSS, Syne + DM Sans fonts, geometric accents  |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure MySQL

Open `config.py` and update:

```python
DB_USER     = "your_mysql_user"
DB_PASSWORD = "your_mysql_password"
DB_NAME     = "nexus_auth"
```

Or set environment variables:

```bash
export DB_USER=root
export DB_PASSWORD=secret
export DB_NAME=nexus_auth
export SECRET_KEY=some-random-secret-key
```

### 3. Create the database

```bash
mysql -u root -p < schema.sql
```

Or let Flask do it automatically on first run.

### 4. Run the app

```bash
python app.py
```

Visit: http://localhost:5000

---

## Project Structure

```
auth_app/
├── app.py                  # Main Flask application
├── config.py               # Configuration
├── schema.sql              # MySQL schema + seed data
├── requirements.txt
├── static/
│   ├── css/style.css       # Full design system
│   └── js/main.js          # Toast auto-dismiss
└── templates/
    ├── base.html           # Base layout + toast system
    ├── login.html          # Sign-in page
    ├── register.html       # Registration with strength meter
    ├── forgot_password.html
    ├── reset_password.html
    ├── dashboard.html      # Post-login dashboard
    └── profile.html        # Profile + password change
```

---

## Security Notes

- All passwords hashed with `werkzeug.security` (bcrypt)
- Account locked after 5 failed login attempts
- Password reset tokens are single-use and expire in 30 minutes
- Session cookie is `HttpOnly` + `SameSite=Lax`
- Email enumeration prevented on forgot-password endpoint
- All form inputs validated server-side (never trust the client)

---

## Optional: Email (SMTP)

Configure in `config.py` or via environment variables:

```
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=you@gmail.com
MAIL_PASSWORD=your_app_password
```

If not configured, the app runs fine — reset links are printed to console.
