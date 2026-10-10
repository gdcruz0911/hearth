// Loaded with `-r` before the app's code: its dialogs take their answers from FAKE_DIALOGS, in order, and each one is
// recorded in globalThis.dialogs. An answer is a button index for a message box, or a path for an open or save dialog.
const { dialog } = require("electron");

const answers = JSON.parse(process.env.FAKE_DIALOGS || "[]");
globalThis.dialogs = [];
const next = (kind, options) => {
  globalThis.dialogs.push({ kind, message: options.message || options.title || "", detail: options.detail || "" });
  if (answers.length === 0) throw new Error(`No answer left for the ${kind} "${options.message || options.title}".`);
  return answers.shift();
};
dialog.showMessageBox = async (...args) => ({ response: next("box", args.at(-1)), checkboxChecked: false });
dialog.showOpenDialog = async (...args) => {
  const path = next("open", args.at(-1));
  return { canceled: !path, filePaths: path ? [path] : [] };
};
dialog.showSaveDialog = async (...args) => {
  const path = next("save", args.at(-1));
  return { canceled: !path, filePath: path || undefined };
};
