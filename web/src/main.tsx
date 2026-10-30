import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router";
import "./index.css";
import ErrorBoundary from "./components/ErrorBoundary";
import WallpaperBackground from "./components/WallpaperBackground";
import ChronologyPage from "./pages/Chronology";
import PresencePage from "./pages/Presence";
import PresenceCatPage from "./pages/PresenceCat";
import CatSettingsPage from "./pages/CatSettings";
import JourneyPage from "./pages/Journey";
import LivePage from "./pages/Live";
import PhotosPage from "./pages/Photos";
import ConfigurationPage from "./pages/Configuration";
import RemoteModeDocPage from "./pages/RemoteModeDoc";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* 07.10, Sid's Shiny -> FastAPI/React migration: one shared multi-page
        bundle now (see vite.config.ts's base="/web-assets/" comment) - every
        migrated slug is a top-level route here instead of its own separate
        build, so a reusable component (BottomNav, the photo lightbox, …)
        only has to be written once. src/webui_api.py serves this exact same
        index.html under every one of these paths. */}
    <ErrorBoundary>
      <BrowserRouter>
        <WallpaperBackground />
        <Routes>
          <Route path="/chronology" element={<ChronologyPage />} />
          <Route path="/presence" element={<PresencePage />} />
          <Route path="/presence/cat/:rfid" element={<PresenceCatPage />} />
          <Route path="/presence/cat/:rfid/edit" element={<CatSettingsPage />} />
          <Route path="/journey" element={<JourneyPage />} />
          <Route path="/live-view" element={<LivePage />} />
          <Route path="/pictures" element={<PhotosPage />} />
          <Route path="/configuration" element={<ConfigurationPage />} />
          <Route path="/remote-mode-doc" element={<RemoteModeDocPage />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
);
