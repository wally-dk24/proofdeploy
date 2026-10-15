"use strict";

// Shared note presentation for the monorepo golden: the wire shape
// of a note lives in the library package, not the app — which is
// why the planted bug (the `bug` revision inverts `done` here)
// breaks the app's responses without the app package changing.
function presentNote(note) {
  return {
    id: note.id,
    title: note.title,
    done: note.done === 1,
    tag: "general",
  };
}

module.exports = { presentNote };
