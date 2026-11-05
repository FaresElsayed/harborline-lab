# Security

Harborline Lab is a practice environment that runs on your own machine. It holds no real data.

- By default the app and database only listen on your own computer (`127.0.0.1`). If you change `HL_BIND_ADDR`, anyone who can reach your machine can try those logins, so keep `.env` private.
- Every password and API key is generated on your machine during setup, and each install gets its own. None ship in this repository, and CI scans for them with gitleaks. Your `.env` is ignored by Git, and an update never changes the keys in it.
- Setup asks for administrator permission only for the steps that need it, such as installing WSL.

If you find a security problem, please report it privately through **Security → Report a vulnerability** on this repository rather than in a public issue.
