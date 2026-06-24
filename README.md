# cloud_system_design
Trying to design a cloud system from scratch.


## Security notes

This project ships with default credentials for local development only:
- Default admin login: `admin` / `admin123`
- Default VM login: `clouduser` / `cloudpass`

**Change both before exposing this system to the internet.**
Set `JWT_SECRET_KEY` in a `.env` file (see `.env.example`) — never commit your real secret key.
