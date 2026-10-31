// Copy to config.js (or run scripts/configure_frontend.sh) and fill in the values for your deployment.
// config.js is generated per deployment and is not committed. Public values only: no secrets here,
// and never a client secret (the SPA uses Authorization Code Flow with PKCE instead).
window.CAMPUS_CAFE_CONFIG = {
  apiBaseUrl: "",      // the stack's ApiUrl output
  auth0Domain: "",     // Auth0 SPA application → Settings → Domain, e.g. "dev-xxxx.us.auth0.com"
  auth0ClientId: "",   // Auth0 SPA application → Settings → Client ID
  auth0Audience: ""    // Auth0 API → Settings → Identifier (same value as the Auth0Audience parameter)
};
