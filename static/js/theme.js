// Classic (non-module) script loaded in <head>: applies the saved theme before
// first paint so there is no flash. The toggle lives in views/dashboard.js.
(function () {
  try {
    var t = localStorage.getItem("bucketeer.theme");
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  } catch (e) {
    /* storage blocked: follow the system theme */
  }
})();
