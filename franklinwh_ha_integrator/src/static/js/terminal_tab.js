/* terminal_tab.js — Diagnostic Console Alpine component */
function diagnosticConsole() {
  return {
    currentInput: '',
    executing: false,
    history: [],
    currentController: null,

    init() {
      setTimeout(() => {
        const input = this.$el.querySelector('input[type="text"]');
        if (input) input.focus();
      }, 100);
    },

    clearConsole() { this.history = []; },

    abortCommand() {
      if (this.currentController) { this.currentController.abort(); this.currentController = null; }
      this.executing = false;
      this.history.push({ type: 'error', text: '^C [Terminated via UI]' });
      this.scrollToBottom();
    },

    scrollToBottom() {
      setTimeout(() => {
        const el = document.getElementById('terminal-output');
        if (el) el.scrollTop = el.scrollHeight;
      }, 50);
    },

    async executeCommand() {
      const cmd = this.currentInput.trim();
      if (!cmd) return;
      const gw = this.$store.app.selectedGateway;
      if (!gw) { this.$store.app.addToast('No gateway selected.', 'error'); return; }
      this.history.push({ type: 'input', text: `franklinwh ${cmd}` });
      this.currentInput = '';
      this.executing = true;
      this.scrollToBottom();
      try {
        this.currentController = new AbortController();
        const r = await fetch('api/terminal/execute', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: cmd, gateway_serial: gw }),
          signal: this.currentController.signal
        });
        const data = await r.json();
        
        let outputText = data.output || '(no output)';
        if (outputText && outputText !== '(no output)') {
          const lines = outputText.split('\n');
          let minIndent = Infinity;
          for (let i = 0; i < lines.length; i++) {
            if (lines[i].trim().length === 0) continue;
            const match = lines[i].match(/^ +/);
            const indent = match ? match[0].length : 0;
            if (indent < minIndent) minIndent = indent;
          }
          if (minIndent > 0 && minIndent !== Infinity) {
            outputText = lines.map(line => line.length >= minIndent ? line.substring(minIndent) : line).join('\n');
          }
        }

        r.ok
          ? this.history.push({ type: 'output', text: outputText })
          : this.history.push({ type: 'error', text: data.detail || 'Execution failed.' });
      } catch (err) {
        if (err.name !== 'AbortError') this.history.push({ type: 'error', text: `Network error: ${err.message}` });
      } finally {
        this.executing = false; this.currentController = null; this.scrollToBottom();
      }
    },

    async generateSupportBundle() {
      if (!this.$store.app.selectedGateway) return;
      this.executing = true;
      this.$store.app.addToast('Generating redacted support bundle...', 'info');
      try {
        let bundleText = '=== FRANKLINWH Integrator Support Bundle ===\n\n';
        for (const cmd of ['diag', 'status', 'metrics']) {
          bundleText += `--- Command: franklinwh ${cmd} ---\n`;
          const r = await fetch('api/terminal/execute', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd, gateway_serial: this.$store.app.selectedGateway })
          });
          const d = await r.json();
          bundleText += (r.ok ? d.output : JSON.stringify(d)) + '\n\n';
        }
        bundleText = bundleText
          .replace(/\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b/g, 'REDACTED@EMAIL.COM')
          .replace(/"token"\s*:\s*"[^"]+"/gi, '"token": "REDACTED"')
          .replace(/(password|secret|key)['"]?\s*[:=]\s*['"]?[^\s'";]+['"]?/gi, '$1: "REDACTED"');
        const a = Object.assign(document.createElement('a'), {
          href: URL.createObjectURL(new Blob([bundleText], { type: 'text/plain;charset=utf-8' })),
          download: `fwh_support_bundle_${Date.now()}.txt`
        });
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        this.$store.app.addToast('Support Bundle downloaded securely.', 'ok');
      } catch (e) { this.$store.app.addToast('Failed to generate bundle.', 'error'); }
      finally { this.executing = false; }
    },
  };
}
