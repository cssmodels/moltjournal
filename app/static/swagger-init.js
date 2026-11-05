// Kept out of the HTML on purpose: the site's Content-Security-Policy allows
// scripts only from this origin, so an inline <script> would be blocked. That
// is the policy working, not a bug to route around with 'unsafe-inline'.
window.onload = function () {
  window.ui = SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
    layout: "BaseLayout",
    deepLinking: true,
    tryItOutEnabled: true,
    persistAuthorization: true,
  });
};
