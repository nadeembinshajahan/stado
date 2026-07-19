import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Preloader from "./components/Preloader";
import ErrorBoundary from "./components/ErrorBoundary";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* Root boundary — last line of defense so any uncaught render throw shows a
        full-screen error card instead of a blank white screen. */}
    <ErrorBoundary label="STRATO·GCS" fullscreen>
      <Preloader />
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
