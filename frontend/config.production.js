// Replaces static/config.js in the production build: the pages call the API on its own
// hostname (a Cloudflare Tunnel to the cluster; see infra/terraform/tunnel.tf).
window.EASYDOCS_API = 'https://easydocs-api.gavinf.com';
