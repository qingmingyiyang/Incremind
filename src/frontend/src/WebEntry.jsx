import { lazy, Suspense, useState } from "react";
import { AppRouter } from "./AppRouter";
import { DeviceGate } from './features/settings/DeviceGate';
const PetApp = lazy(() => import("./App").then((module) => ({ default: module.App })));

export function WebEntry() {
  const [surface] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("view") === "rebuild-pet" ? "pet" : "main");
  return surface === "pet"
    ? <Suspense fallback={null}><PetApp initialSurface="pet" /></Suspense>
    : <DeviceGate><AppRouter /></DeviceGate>;
}
