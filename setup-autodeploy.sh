#!/bin/bash
# Auto-deployment setup script for Arch Linux

echo "=== Yuzone API Auto-Deployment Setup ==="
echo ""

# 1. Setup systemd service
echo "Step 1: Setting up systemd service..."
echo "Edit yuzone-api.service and replace YOUR_USERNAME and YOUR_RAPIDAPI_KEY"
echo "Then run:"
echo "  sudo cp yuzone-api.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable yuzone-api"
echo "  sudo systemctl start yuzone-api"
echo "  sudo systemctl status yuzone-api"
echo ""

# 2. Allow user to restart service without password
echo "Step 2: Allow your user to restart the service without sudo password"
echo "Run this command (replace YOUR_USERNAME):"
echo "  echo 'YOUR_USERNAME ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart yuzone-api' | sudo tee /etc/sudoers.d/yuzone-api"
echo "  sudo chmod 0440 /etc/sudoers.d/yuzone-api"
echo ""

# 3. Setup GitHub webhook
echo "Step 3: Setup GitHub webhook"
echo "1. Go to: https://github.com/Agnibha007/Yuzone_API/settings/hooks"
echo "2. Click 'Add webhook'"
echo "3. Set Payload URL to: https://your-cloudflare-url.trycloudflare.com/webhook/deploy"
echo "4. Set Content type: application/json"
echo "5. Set Secret (optional): generate a random string and set as GITHUB_WEBHOOK_SECRET env var"
echo "6. Select 'Just the push event'"
echo "7. Click 'Add webhook'"
echo ""

# 4. Environment variables
echo "Step 4: Set environment variables (if using webhook secret)"
echo "Add to yuzone-api.service under [Service]:"
echo '  Environment="GITHUB_WEBHOOK_SECRET=your_random_secret_here"'
echo "Then reload: sudo systemctl daemon-reload && sudo systemctl restart yuzone-api"
echo ""

# 5. Test
echo "Step 5: Test the setup"
echo "  git commit -am 'test auto-deploy' && git push"
echo "  Watch logs: sudo journalctl -fu yuzone-api"
echo ""

echo "=== Setup Complete! ==="
echo "Now whenever you push to master, the server will auto-update and restart."
