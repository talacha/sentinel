// Where the Sentinel API lives.
//
// Leave apiBase empty when this app is served by the API itself (http://host:8000/app/): it then
// talks to the same origin. When you host this folder somewhere else, set it to the API's URL,
// e.g. "https://sentinel.example.com", and set CORS_ALLOW_ORIGINS on the API to this app's origin.
window.SENTINEL_CONFIG = {
  apiBase: "",

  // Local development only. When true, a link like ?api=http://localhost:8000 overrides apiBase.
  // Keep it false anywhere real: otherwise a crafted link could make a visitor upload their
  // documents to someone else's server.
  allowApiParam: false,
};
