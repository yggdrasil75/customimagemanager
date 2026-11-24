/* vlm: AI buttons in the book editor.
 * The books module offers registerBookTool(slot, spec); when it is there (books is
 * on) the AI tags and AI blurb buttons go in. A script order where this file runs
 * first waits for books' `cim:book-tools` event. Without the books module nothing
 * happens. The routes are this module's own (book_tools.py). */
(function () {
  let done = false;
  function add() {
    if (done || typeof window.registerBookTool !== 'function') return;
    done = true;
    registerBookTool('tags', { id: 'vlm_book_tags', label: 'AI', url: '/api/vlm/book/tags',
      variant: 'warn', feature: 'ai.llm', busy: '...',
      title: 'Suggest tags (genre, themes, setting, audience) with the chat model' });
    registerBookTool('description', { id: 'vlm_book_describe', label: 'AI', url: '/api/vlm/book/describe',
      variant: 'warn', feature: 'ai.llm', busy: '...',
      title: 'Write a jacket blurb with the chat model' });
  }
  add();
  if (!done) window.addEventListener('cim:book-tools', add, { once: true });
})();