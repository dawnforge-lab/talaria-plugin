(function () {
  "use strict";
  // talaria_push has no dashboard UI; it only mounts backend routes for the
  // Talaria app. Register an empty component so the dashboard doesn't
  // report the plugin as failing to load.
  if (!window.__HERMES_PLUGINS__) return;
  window.__HERMES_PLUGINS__.register("talaria_push", function () {
    return null;
  });
})();
