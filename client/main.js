// Speedy desktop client (Electron).
//
// A thin wrapper around the Speedy web app. It loads the bundled connect
// screen, which navigates to the user's own Speedy server. Downloads always
// happen on the server machine, never inside this window.
const { app, BrowserWindow, shell } = require("electron");
const path = require("node:path");

function createWindow() {
  const win = new BrowserWindow({
    width: 1100,
    height: 780,
    minWidth: 380,
    minHeight: 560,
    backgroundColor: "#0b0d12",
    title: "Speedy",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  win.loadFile(path.join(__dirname, "www", "index.html"));

  // Open external links (WhatsApp, tel, help pages) in the system browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  // The app navigates to whatever server the user enters, so allow any http(s)
  // target here; block every other scheme.
  win.webContents.on("will-navigate", (event, url) => {
    if (!/^https?:\/\//i.test(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  return win;
}

app.on("ready", createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
