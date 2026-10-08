import { AppRouter } from "./AppRouter";
import { lazy, useState } from "react";
const DesktopPet = lazy(() => import("./features/rebuild/DesktopPet").then(module => ({ default: module.DesktopPet })));

export function App({ initialSurface } = {}) {
  const [surface] = useState(() => initialSurface === "pet" || (initialSurface == null && new URLSearchParams(window.location.hash.slice(1)).get("view") === "rebuild-pet") ? "pet" : "main");
  return surface === "pet" ? <DesktopPet /> : <AppRouter />;
}
