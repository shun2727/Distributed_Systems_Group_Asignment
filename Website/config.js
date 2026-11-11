// config.js: the only two settings you may ever need to change.

const CONFIG = {
  // Where the Booking service is running. The client talks ONLY to this address.
  // Keeping it here (not scattered through the code) is what lets you say in the
  // report: "moving a service only requires changing its base URL".
  API_BASE: "http://localhost:5001",

  // true  = use fake in-browser data (js/mock.js), so you can try the page before
  //         the real Booking service is finished.
  // false = send real HTTP requests to API_BASE.
  USE_MOCK: true,
};
