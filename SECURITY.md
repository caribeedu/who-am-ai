# Security policy

who-am-ai reads your assistant transcripts (today, Claude Code's) and holds an Anthropic API key, so security reports are taken seriously.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability reporting: open the **Security** tab of the repository and choose **Report a vulnerability**.

Include what you found, how to reproduce it and the impact you expect. You will get an acknowledgement, and fixes are prioritized by severity.

## Design notes relevant to security

- Everything runs locally as a one-shot command. There is no server and nothing listens on a port.
- The only data that leaves the machine are your messages, and the tail of the assistant replies they react to, sent to the Anthropic API to annotate preferences. Pasted material is replaced by a marker before anything is sent.
- The API key is stored in the system keychain (the macOS Keychain, the Windows Credential Manager or the Linux Secret Service), never in files or in the state database.
- State (`~/.who-am-ai/`) holds your messages' literal quotes as evidence. Treat it like your transcripts.
