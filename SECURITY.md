# Security policy

Local AI Connector mediates task approval between desktop AI agents and stores local credentials, so security reports are especially welcome.

## Reporting a vulnerability

Please report privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request or discussion for a suspected vulnerability.

Include the affected version or commit, the steps to reproduce, the host apps and versions involved, and the impact you observed. Redact real chat, session or project IDs, credentials and home-directory paths. This is a volunteer project: we will acknowledge reports as soon as we can and coordinate a fix and disclosure with you.

## Supported versions

Only the latest commit on the default branch is supported during the public preview.

## In scope

- A task becoming visible to a worker without approval in the initiating desktop, or approval obtained through tool arguments, model output or an error path.
- Delivery to a chat other than the registered target, or follow-up rounds that extend or bypass the original authorization.
- A chat other than the one a task is pinned to receiving or answering it, or a binding made without the host-supplied chat identity.
- Disclosure of endpoint or approval credentials, or access to another endpoint's channels.
- Command execution through wake-up configuration, bridges or deployment scripts beyond what the owner configured.
- Bypassing the local-only HTTP protections (Origin and Host checks, Bearer authentication).

## Known limitations (not vulnerabilities by themselves)

- Processes running as the same OS user can read the data directory; file permissions are not isolation against them.
- The controlled file-write lock only applies to the connector's own write path.
- Host integrations depend on non-public desktop interfaces that may change.

---

## 中文说明

请勿公开提交安全问题。请在 GitHub 仓库的 **Security → Report a vulnerability** 私下报告，并附上版本、复现步骤和涉及的宿主版本，同时隐去真实 ID、凭据和本机路径。

公开预览期间只支持默认分支的最新提交。

重点范围：
- 绕过发起端批准
- 投递到非登记聊天
- 凭据泄露或越权访问
- 经唤醒配置或脚本执行未授权命令
- 绕过本机 HTTP 防护
