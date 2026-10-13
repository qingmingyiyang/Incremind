import { lazy, Suspense, useEffect, useState } from "react";
import { AppRouter } from "./AppRouter";
import { DeviceGate } from './features/settings/DeviceGate';
import { CaptureEntry } from './features/capture/CaptureEntry';
const PetApp = lazy(() => import("./App").then((module) => ({ default: module.App })));

function readSurface() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  return params.get('view') === 'rebuild-pet' ? 'pet' : params.has('capture') || params.has('capture-error') ? 'capture' : 'main';
}
export function WebEntry() {
  const [surface, setSurface] = useState(readSurface);
  useEffect(() => {
    const changed = () => setSurface(readSurface());
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);
  return surface === "pet"
    ? <Suspense fallback={null}><PetApp initialSurface="pet" /></Suspense>
    : <DeviceGate>{surface === 'capture' ? <CaptureEntry /> : <AppRouter />}</DeviceGate>;
}
