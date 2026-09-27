import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { FailoverPage } from "./pages/Failover";

const degraded =
  document.documentElement.dataset.coireTier === "failover" || window.location.hash === "#failover";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>{degraded ? <FailoverPage /> : <App />}</React.StrictMode>,
);
