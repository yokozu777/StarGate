# Setting Up a Git Repository for Your Project

This guide explains how to prepare a Git repository and connect it to a Stargate project so you can pull and push playbooks, roles, and inventories.

---

## What Gets Synced

Stargate syncs these folders between your Git repo and the project:

| Folder         | Contents                          |
|----------------|-----------------------------------|
| `playbooks/`   | Ansible playbooks                 |
| `roles/`       | Ansible roles                     |
| `inventories/` | Inventory files, group_vars, host_vars |
| `vars/`        | Shared variables (optional)       |

Root-level files (e.g. `ansible.cfg`) are also synced.

**Ansible Vault:** Encrypted files (e.g. `group_vars/all/vault.yml`, `vars/secrets.yml`) are synced — the encrypted content goes to Git. Vault keys and passwords are stored only in Stargate (see [Vault](#vault)).

---

## Recommended Repository Structure

Use this layout in your Git repository:

```
your-ansible-repo/
├── playbooks/
│   └── site.yml
├── roles/
│   └── common/
├── inventories/
│   ├── inventory.yml
│   ├── prod/
│   │   ├── inventory.yml
│   │   ├── group_vars/
│   │   └── host_vars/
│   └── dev/
│       ├── inventory.yml
│       ├── group_vars/
│       └── host_vars/
├── vars/              # optional
└── ansible.cfg        # optional
```

### Inventory Layout

Each inventory can have its own `group_vars/` and `host_vars/`. Supported inventory file names: `inventory.yml`, `inventory.yaml`, `hosts.yml`, `hosts.yaml`, `hosts`, `hosts.ini`.

```
inventories/
├── inventory.yml           # or hosts, hosts.ini, hosts.yml, hosts.yaml
├── prod/
│   ├── inventory.yml
│   ├── group_vars/
│   └── host_vars/
└── dev/
    ├── hosts.ini
    ├── group_vars/
    └── host_vars/
```

### Example: inventory.yml (YAML)

```yaml
all:
  hosts:
    web1:
      ansible_host: 192.168.1.10
    web2:
      ansible_host: 192.168.1.11
  children:
    webservers:
      hosts:
        web1:
        web2:
```

### Example: hosts or hosts.ini (INI)

```ini
[webservers]
web1 ansible_host=192.168.1.10
web2 ansible_host=192.168.1.11
```

---

## Initial Setup

### 1. Create or Prepare Your Repository

- Create a new repo (GitHub, GitLab, Gitea, etc.) or use an existing one.
- Ensure it has the structure above (at least `playbooks/`, `roles/`, `inventories/`).
- Push your content to the default branch (e.g. `main`).

### 2. Configure the Project in Stargate

1. Open your project in Stargate.
2. Go to **Sources** (or **Project Settings → Sources**).
3. Set **Source** to **Git**.
4. Fill in:
   - **Repository URL** — e.g. `git@github.com:org/ansible-repo.git` or `https://github.com/org/ansible-repo.git`
   - **Branch** — e.g. `main` or `master`
   - **Authentication** — if the repo is private, select or create a secret (SSH key or token)

### 3. Choose Sync Direction

- **Pull only** — Stargate only fetches from Git (read-only).
- **Push only** — Stargate only pushes changes to Git.
- **Both** — You can pull and push.

For most workflows, **Both** is recommended.

### 4. Test Connection

Use **Test connection** to verify Stargate can clone the repo. Fix any auth or URL errors before continuing.

### 5. First Pull

Click **Pull** to sync the repository into the project. After that, playbooks, roles, and inventories will appear in the UI.

---

## Monorepo: Using a Subdirectory

If your Ansible content lives in a subfolder (e.g. `ansible/` or `infra/`), set **Subdirectory**:

```
your-monorepo/
├── app/
├── ansible/           ← subdirectory
│   ├── playbooks/
│   ├── roles/
│   └── inventories/
└── README.md
```

In Stargate, set **Subdirectory** to `ansible`. Stargate will use `ansible/` as the base for playbooks, roles, and inventories.

---

## Custom Folder Names (Ansible Entity Paths)

If your repo uses different folder names (e.g. `inv` instead of `inventories`), configure **Ansible Entity Paths**:

| Entity      | Default path  | Example custom |
|-------------|---------------|-----------------|
| Playbooks   | `playbooks`   | `playbooks`     |
| Roles       | `roles`       | `roles`         |
| Inventories | `inventories`| `inv`           |
| Vars        | `vars`        | `vars`          |

Set the custom path for each entity. Stargate will map it to the standard structure in the project.

---

## Role Configurator (Frontend Configuration)

To configure role variables from the Stargate UI (Role Configurator), the role must have a `defaults/main.yml` or `defaults/main.yaml` file.

### Requirement

Create `defaults/main.yml` (or `defaults/main.yaml`) in your role:

```
roles/
└── my_role/
    ├── tasks/
    │   └── main.yml
    └── defaults/
        └── main.yml    ← required for UI configuration
```

### Example: defaults/main.yml

```yaml
---
# Variables with defaults; these appear in the Role Configurator UI
timezone: UTC
ntp_servers:
  - time.nist.gov
  - pool.ntp.org
```

Without `defaults/main.yml` (or `defaults/main.yaml`), the Role Configurator will show "Defaults not found" and variable configuration will not be available. Add the file, push to Git, pull in Stargate, and the configuration UI will appear.

---

## Vault

Stargate supports Ansible Vault for encrypting sensitive variables.

### What syncs with Git

- **Vault-encrypted files** — Files encrypted with `ansible-vault` (e.g. in `group_vars/`, `host_vars/`, `vars/`) are synced. The encrypted content is safe to store in Git.

### What stays in Stargate

- **Vault keys and passwords** — Created in the **Vaults** tab. Never stored in Git.
- **Vault configurations** — Vault names, IDs, and key bindings.

### Workflow

1. In Stargate, go to **Vaults** and create a **Vault Key** (password).
2. Create a **Vault** and link it to that key.
3. Edit or create vault-encrypted files in the UI (inventory, group_vars, host_vars, or vars).
4. Save — Stargate encrypts with the vault key. The encrypted file syncs to Git on push.
5. On pull, encrypted files come from Git; Stargate decrypts them using the stored key when you open them in the UI.

If you clone the project on another Stargate instance, recreate the vault and key there — keys are not in Git.

---

## Authentication

### Public Repositories

No authentication needed. Enter the URL and branch, then test and pull.

### Private Repositories

1. Create a secret in Stargate (Project or Global secrets):
   - **SSH** — paste your private key.
   - **HTTPS** — use a personal access token or password.
2. In Sources, select this secret for **Authentication**.
3. Use the matching URL:
   - SSH: `git@github.com:org/repo.git`
   - HTTPS: `https://github.com/org/repo.git` (token used automatically)

---

## What Is Not Synced

These are stored only in Stargate and never pushed to Git:

- **Secrets** — SSH keys, vault keys/passwords, Git auth
- **Execution history** — logs and run results
- **UI state** — playbook selections, worker assignments

Keep credentials and sensitive data in Stargate secrets, not in the Git repo.

---

## Pull and Push Workflow

- **Pull** — Fetches the latest from Git and updates the project. Use this when someone else changed the repo or you edited it outside Stargate.
- **Push** — Commits your changes in Stargate and pushes to Git. Use this after editing playbooks, roles, or inventories in the UI.

If push fails (e.g. remote has new commits), pull first, then push again.

---

## Troubleshooting

| Problem | What to check |
|---------|----------------|
| Test connection fails | URL, branch, and authentication (key/token) |
| Pull returns empty | Repo structure (playbooks/, roles/, inventories/); subdirectory if used |
| Push rejected | Pull first; ensure you have write access to the repo |
| Auth error on private repo | Secret type (SSH vs HTTPS) matches URL; key has no passphrase or passphrase is in secret |
