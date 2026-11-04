# Security

Harborline Lab is a practice environment that runs on your own machine. It holds no real data.

- By default the app and database only listen on your own computer (`127.0.0.1`). If you change `HL_BIND_ADDR`, change the passwords in `.env` too.
- The passwords in `.env.example` are sample values for local use, so a secret scanner may flag them. Your own `.env` is ignored by Git.
- Setup asks for administrator permission only for the steps that need it, such as installing WSL.

If you find a security problem, please report it privately through **Security → Report a vulnerability** on this repository rather than in a public issue.
