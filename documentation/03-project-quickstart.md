# Project Quick Start

Quick guide to creating a project (with Git already connected) and running a playbook.

**Prerequisites:** Git repository is configured in Sources and content has been pulled.

---

## 1. Create a Project

1. Open Stargate and go to the **Projects**.
2. Click **Create Project**.
3. Enter:
   - **Name** — e.g. `My Infrastructure`
   - **Description** — optional
4. Click **Create**.

---

## 2. Connect Git (if not done)

See [04-repository-structure-git-sync.md](04-repository-structure-git-sync.md) for details.

1. Select the new project from the sidebar.
2. Go to **Project Settings** → **Sources**.
3. Set **Source** to **Git**.
4. Fill in **Repository URL**, **Branch**, and **Authentication** (if private).
5. Click **Test connection**, then **Pull**.

After pull, playbooks, roles, and inventories from the repo appear in the project.

---

## 3. Configure Inventory and SSH (if needed)

1. Go to **Infrastructure** → **Hosts & Groups**.
2. Select inventory files (e.g. `inventory.yml`).
3. For each host or group that needs SSH, assign an **SSH key** secret (Project Settings → Secrets).

---

## 4. Run a Playbook

1. Go to **Runs**.
2. Find the playbook in the list.
3. Click **Run**.
4. In the Run Playbook form:
   - Select **hosts** or groups.
   - Select **roles** (if the playbook uses them).
   - Adjust options (check mode, verbosity, etc.) if needed.
5. Click **Run** to start the execution.

The run appears in the list; expand the row to see logs and status.

---

## Summary

| Step | Where | Action |
|------|-------|--------|
| 1 | Dashboard | Create Project |
| 2 | Project Settings → Sources | Git: URL, branch, Pull |
| 3 | Hosts & Groups | Select inventory, SSH keys |
| 4 | Runs | Select playbook → Run → choose hosts/roles → Run |
