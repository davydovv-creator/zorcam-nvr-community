# Security policy

## Reporting a vulnerability

Please report security problems by e-mail to **davydovv@gmail.com**. Do not open a public issue or a pull request for
a vulnerability: the details would be visible to everyone before a fix exists.

Include what you found, the version (Settings → Status shows it, or `git describe --tags`), and the steps to reproduce.
We aim to reply within 7 days. Please give us a reasonable time to fix the problem before you publish it.

## Supported versions

Only the latest release gets security fixes. Update by running `install.sh` on a fresh copy of the program
(see "Update" in the README).

## How it is built

- Zorcam runs on your own device in your home network. Recordings, snapshots and camera passwords stay on it.
- The gallery needs a sign-in (users and roles); the go2rtc API and the RTSP server listen on `127.0.0.1` only.
- The Zorcam cloud is optional and off by default. Without it nothing is sent to Zorcam servers; it never gets the
  camera or gallery passwords, recordings or snapshots.
- The full list of outbound connections, and how to check it yourself, is in the README, "Outbound connections".
  There is no telemetry and nothing updates itself.
