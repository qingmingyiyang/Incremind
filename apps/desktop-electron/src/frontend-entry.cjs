const DEFAULT_PET_VIEW = "rebuild-pet";

function hashView(url) {
  return new URLSearchParams(url.hash.replace(/^#/, "")).get("view") || "";
}

function resolveFrontendEntry({ devUrl = "", petView = false, frontendIndex }) {
  if (devUrl) {
    const url = new URL(devUrl);
    const requestedMainView = hashView(url) || url.searchParams.get("view") || "home";
    const view = petView ? DEFAULT_PET_VIEW : requestedMainView;
    url.searchParams.delete("view");
    url.hash = new URLSearchParams({ view }).toString();
    return { type: "url", value: url.toString() };
  }
  return {
    type: "file",
    value: frontendIndex,
    hash: `view=${petView ? DEFAULT_PET_VIEW : "home"}`,
  };
}

module.exports = { DEFAULT_PET_VIEW, resolveFrontendEntry };
