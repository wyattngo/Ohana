# GitHub Actions Secrets Setup Guide

This file documents the secrets required for the CI/CD pipeline.

## Setting up GitHub Secrets

1. Go to your GitHub repository
2. **Settings → Secrets and variables → Actions**
3. Create each secret below

## Required Secrets

### Docker Registry (DOCKERHUB_USERNAME, DOCKERHUB_TOKEN)

Used to push built images to Docker Hub.

**Steps:**
1. Create Docker Hub account (hub.docker.com)
2. Generate access token:
   - Docker Hub → Account Settings → Security → New Access Token
   - Scope: Read & Write
3. Add secrets:
   ```
   DOCKERHUB_USERNAME = your-dockerhub-username
   DOCKERHUB_TOKEN = your-dockerhub-token
   ```

### SSH Deploy Key (DEPLOY_KEY, DEPLOY_HOST, DEPLOY_USER, DEPLOY_PORT)

Used to SSH into production/staging servers for deployment.

**Steps:**

1. Generate SSH key on your machine (if you don't have one):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
   ```

2. Create SSH deploy key (separate from your personal key):
   ```bash
   ssh-keygen -t ed25519 -f /tmp/deploy_key -N ""
   ```

3. Add public key to target server:
   ```bash
   cat /tmp/deploy_key.pub | ssh deploy@prod.example.com \
       "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```

4. Get private key in base64 format:
   ```bash
   cat /tmp/deploy_key | base64 -w0 | xclip -selection clipboard
   ```

5. Add secrets to GitHub:
   ```
   DEPLOY_KEY = (paste base64-encoded private key)
   DEPLOY_HOST = prod.example.com  (or IP address)
   DEPLOY_USER = deploy  (or your SSH user)
   DEPLOY_PORT = 22  (or custom SSH port)
   ```

## Environment-specific Secrets (Optional)

If you want different settings for staging vs production:

1. Create GitHub environments:
   - **Settings → Environments → New environment**
   - Name: `staging` and `production`

2. Add environment secrets (overrides for each):
   ```
   Environment: staging
   - DEPLOY_HOST = staging.example.com
   - DEPLOY_USER = deploy
   
   Environment: production
   - DEPLOY_HOST = prod.example.com
   - DEPLOY_USER = deploy
   ```

## Verification

Test that secrets are configured correctly:

```bash
# Local: verify you can SSH without password
ssh -i /tmp/deploy_key deploy@prod.example.com "echo Connected"

# GitHub: check workflow logs
# Go to Actions → CI → click failed job → see actual error
```

## Security Best Practices

- ✓ Rotate deploy keys every 6 months
- ✓ Use a dedicated `deploy` user with limited permissions
- ✓ Never commit `.env.prod` or private keys
- ✓ Use separate keys for each environment
- ✓ Monitor secret access in GitHub audit logs

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Permission denied (publickey)" | Verify public key added to `~/.ssh/authorized_keys` on server, check file permissions (600) |
| "Invalid base64" | Ensure key is base64 encoded: `base64 -w0 /tmp/deploy_key` |
| "Docker image not found" | Verify `DOCKERHUB_USERNAME` is correct, token has read/write access |
| Workflow doesn't run | Ensure branch is `main`, check workflow file syntax with `yamllint` |
