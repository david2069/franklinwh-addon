/* security_tab.js — Security tab controller (BKL-SEC-01) */
function securityTab() {
  return {
    loading: false,
    status: {
      security_enabled: false,
      tls_enabled: false,
      mtls_enabled: false,
      tls_cert_path: '/data/ssl/server.crt',
      tls_key_path: '/data/ssl/server.key',
      client_ca_path: '/data/ssl/ca.crt',
      // {} not null: 25 bindings read status.cert_info.<field> directly, and
      // until loadStatus() resolves every one of them threw
      // "null is not an object" into the console on each page load. An empty
      // object yields undefined instead, which every guard here already treats
      // as absent — `cert_info && cert_info.valid` and `!cert_info.valid` both
      // behave exactly as they did with null.
      cert_info: {},
      client_ca_info: null,
      active_api_tokens: [],
      admin_username: 'admin',
      setup_complete: false,
      tamper_check_passed: true,
      ha_token_configured: false
    },
    
    // Wizard state
    wizardStep: 1,
    newUsername: 'admin',
    newPassword: '',
    newPasswordConfirm: '',
    enableSecurityCheckbox: false,
    enableTlsCheckbox: false,
    enableMtlsCheckbox: false,
    tlsCertPath: '/data/ssl/server.crt',
    tlsKeyPath: '/data/ssl/server.key',
    clientCaPath: '/data/ssl/ca.crt',

    // API Token generation state
    tokenName: '',
    tokenExpiresInDays: 30,
    generatedTokenResult: null,
    showTokenModal: false,
    showDisableSecurityModal: false,

    // Sub-tab state. 'overview' is the landing panel; the others are reached
    // by the nav. Everything loads on init(), so switching panels costs no
    // request and going back is instant.
    activeSubTab: 'overview',

    // MFA enrolment state. currentUser is stamped into the page by the server
    // (same source as the topbar chip) because only the signed-in user can
    // enrol themselves — an admin has no way to scan someone else's QR.
    currentUser: (window.FHAI_CURRENT_USER || ''),
    tlsSaving: false,

    // Wizard visibility, kept separate from server state.
    //
    // The wizard used to render unconditionally, below the "Security Suite
    // Active" card — so the page simultaneously said security was on and
    // offered to set it up, describing the app as running "in unsecured local
    // mode with no credentials forced". Reconfigure Security worked around it
    // by assigning status.security_enabled = false, which is a lie about server
    // state that the next loadStatus() would silently undo.
    showWizard: false,
    showMfaModal: false,
    mfaLoading: false,
    mfaSetup: null,
    mfaCode: '',

    // User management state
    usersList: [],
    showAddUserForm: false,
    newProfileUsername: '',
    newProfilePassword: '',
    newProfileRole: 'user',
    newProfileDashboard: 'standard',
    newProfileEmail: '',
    showChangePasswordModal: false,
    changePasswordTargetUser: '',
    changePasswordNewValue: '',
    changePasswordConfirmValue: '',

    // Edit user profile state
    showEditUserModal: false,
    editProfileUsername: '',
    editProfileRole: 'user',
    editProfileDashboard: 'standard',
    editProfileEmail: '',

    openEditUserModal(user) {
      this.editProfileUsername = user.username;
      this.editProfileRole = user.role;
      this.editProfileDashboard = user.dashboard;
      this.editProfileEmail = user.email || '';
      this.showEditUserModal = true;
    },

    async submitEditUser() {
      this.loading = true;
      try {
        const r = await fetch(`api/security/users/${this.editProfileUsername}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            role: this.editProfileRole,
            dashboard: this.editProfileDashboard,
            email: this.editProfileEmail.trim() || null
          })
        });
        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast(`User profile '${this.editProfileUsername}' updated successfully.`, 'info');
          this.showEditUserModal = false;
          await this.loadUsers();
        } else {
          this.$store.app.addToast(data.detail || 'Failed to update user profile.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Failed to update user profile: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async init() {
      await Promise.all([this.loadStatus(), this.loadUsers()]);
    },

    async loadStatus() {
      this.loading = true;
      try {
        const r = await fetch('api/security/status');
        if (r.ok) {
          const data = await r.json();
          this.status = data;
          
          // Seed form/wizard fields from current active config
          this.newUsername = data.admin_username || 'admin';
          this.enableSecurityCheckbox = data.security_enabled;
          this.enableTlsCheckbox = data.tls_enabled;
          this.enableMtlsCheckbox = data.mtls_enabled;
          this.tlsCertPath = data.tls_cert_path;
          this.tlsKeyPath = data.tls_key_path;
          this.clientCaPath = data.client_ca_path;
        } else {
          this.$store.app.addToast('Failed to load security status.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Failed to load security status: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async nextStep() {
      if (this.wizardStep === 2) {
        // Validate password changes
        if (this.newPassword || !this.status.setup_complete) {
          if (!this.newPassword) {
            this.$store.app.addToast('Please enter an administrator password.', 'error');
            return;
          }
          if (this.newPassword.length < 8) {
            this.$store.app.addToast('Password must be at least 8 characters long.', 'error');
            return;
          }
          if (this.newPassword !== this.newPasswordConfirm) {
            this.$store.app.addToast('Passwords do not match.', 'error');
            return;
          }
        }
      }
      this.wizardStep++;
    },

    prevStep() {
      if (this.wizardStep > 1) {
        this.wizardStep--;
      }
    },

    async saveWizardSetup() {
      this.loading = true;
      try {
        const payload = {
          username: this.newUsername,
          security_enabled: this.enableSecurityCheckbox,
          tls_enabled: this.enableTlsCheckbox,
          mtls_enabled: this.enableMtlsCheckbox,
          tls_cert_path: this.tlsCertPath,
          tls_key_path: this.tlsKeyPath,
          client_ca_path: this.clientCaPath
        };

        if (this.newPassword) {
          payload.password = this.newPassword;
        }

        const r = await fetch('api/security/setup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast('Security settings successfully updated!', 'info');
          // Clear sensitive fields
          this.newPassword = '';
          this.newPasswordConfirm = '';
          
          await this.loadStatus();
          this.wizardStep = 1; // reset step
          // Setup is done — go back to the status card. Leaving the wizard open
          // over a configured instance is what made it look permanent.
          this.showWizard = false;
          
          // If user changed security parameters (like enabling TLS), they might need to reload or re-authenticate
          if (payload.security_enabled && !this.status.security_enabled) {
            this.$store.app.addToast('Security Mode is now ACTIVE. Redirecting to login...', 'info');
            setTimeout(() => {
              window.location.reload();
            }, 2000);
          }
        } else {
          this.$store.app.addToast(data.detail || 'Setup configuration failed.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Configuration failed: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async uploadCertFile(event, type) {
      const file = event.target.files[0];
      if (!file) return;

      this.loading = true;
      try {
        const formData = new FormData();
        formData.append(type, file);

        const r = await fetch('api/security/certificates', {
          method: 'POST',
          body: formData
        });

        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast(data.message || 'File uploaded successfully!', 'info');
          await this.loadStatus();
        } else {
          this.$store.app.addToast(data.detail || 'Upload failed.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Upload failed: ${e.message}`, 'error');
      } finally {
        this.loading = false;
        // reset input
        event.target.value = '';
      }
    },

    async generateToken() {
      if (!this.tokenName.trim()) {
        this.$store.app.addToast('Please enter a descriptive token name.', 'error');
        return;
      }

      this.loading = true;
      try {
        const r = await fetch('api/security/tokens', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: this.tokenName,
            expires_in_days: parseInt(this.tokenExpiresInDays) || null
          })
        });

        const data = await r.json();
        if (r.ok) {
          this.generatedTokenResult = data.token;
          this.showTokenModal = true;
          this.tokenName = '';
          this.$store.app.addToast('API Token generated successfully!', 'info');
          await this.loadStatus();
        } else {
          this.$store.app.addToast(data.detail || 'Token generation failed.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Token generation failed: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async revokeToken(tokenId) {
      if (!await this.$store.app.confirm({
        title: 'Revoke this API token?',
        body: 'Any integration using it stops working immediately.',
        confirmLabel: 'Revoke', danger: true,
      })) {
        return;
      }

      this.loading = true;
      try {
        const r = await fetch(`api/security/tokens/${tokenId}`, {
          method: 'DELETE'
        });

        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast('API Token successfully revoked.', 'info');
          await this.loadStatus();
        } else {
          this.$store.app.addToast(data.detail || 'Revocation failed.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Revocation failed: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async disableSecurity() {
      this.loading = true;
      try {
        const r = await fetch('api/security/setup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            security_enabled: false,
            tls_enabled: false,
            mtls_enabled: false
          })
        });

        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast('Full security suite disabled. Reverted to unsecured local HTTP mode.', 'warn');
          this.showDisableSecurityModal = false;
          await this.loadStatus();
          // Clear session cookies and reload
          await fetch('api/security/logout', { method: 'POST' });
          setTimeout(() => {
            window.location.reload();
          }, 1500);
        } else {
          this.$store.app.addToast(data.detail || 'Deactivation failed.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Deactivation failed: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async loadUsers() {
      this.loading = true;
      try {
        const r = await fetch('api/security/users');
        if (r.ok) {
          this.usersList = await r.json();
        } else {
          // If security isn't enabled or user isn't admin, it could return 401/403.
          // Don't spam toasts if security is disabled.
          if (this.status.security_enabled) {
            this.$store.app.addToast('Failed to load user profiles.', 'error');
          }
        }
      } catch (e) {
        console.warn('Failed to load user profiles:', e);
      } finally {
        this.loading = false;
      }
    },

    async createUserProfile() {
      if (!this.newProfileUsername.trim()) {
        this.$store.app.addToast('Please enter a username.', 'error');
        return;
      }
      if (this.newProfilePassword.length < 8) {
        this.$store.app.addToast('Password must be at least 8 characters long.', 'error');
        return;
      }

      this.loading = true;
      try {
        const r = await fetch('api/security/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: this.newProfileUsername.trim(),
            password: this.newProfilePassword,
            role: this.newProfileRole,
            dashboard: this.newProfileDashboard,
            email: this.newProfileEmail.trim() || null
          })
        });
        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast(`User profile '${this.newProfileUsername}' created successfully.`, 'info');
          this.newProfileUsername = '';
          this.newProfilePassword = '';
          this.newProfileEmail = '';
          this.showAddUserForm = false;
          await this.loadUsers();
        } else {
          this.$store.app.addToast(data.detail || 'Failed to create user profile.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Failed to create user profile: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    async deleteUserProfile(username) {
      if (username === this.status.admin_username) {
        this.$store.app.addToast('Cannot delete your own active administrator account.', 'error');
        return;
      }
      if (!await this.$store.app.confirm({
        title: `Delete the profile '${username}'?`,
        body: 'They lose access immediately. This cannot be undone.',
        confirmLabel: 'Delete', danger: true,
      })) {
        return;
      }

      this.loading = true;
      try {
        const r = await fetch(`api/security/users/${username}`, {
          method: 'DELETE'
        });
        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast(`User profile '${username}' deleted successfully.`, 'info');
          await this.loadUsers();
        } else {
          this.$store.app.addToast(data.detail || 'Failed to delete user profile.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Failed to delete user profile: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    /** Restart the app so the TLS change takes effect, and send the browser to
     *  the scheme it will be answering on.
     *
     *  The redirect matters: this very page was loaded over the scheme that is
     *  about to stop working, so simply reloading would fail and look like the
     *  restart broke something. The delay is for the process to actually go
     *  down and be respawned by the container's restart policy.
     */
    async restartForTls(tlsTarget) {
      try {
        await fetch('api/system/restart', { method: 'POST' });
      } catch (_) {
        // The connection dropping IS the restart — not an error worth showing.
      }
      this.$store.app.addToast('Restarting… this page will follow in a few seconds.', 'info',
        { autoDismiss: false });

      const scheme = tlsTarget ? 'https:' : 'http:';
      setTimeout(() => {
        if (window.location.protocol === scheme) {
          window.location.reload();
        } else {
          window.location.protocol = scheme;
        }
      }, 6000);
    },

    /** Reopen the setup wizard on an instance that is already configured.
     *
     * Re-seeds every control from the current status first: the wizard's
     * checkboxes are working copies, and a previous visit that was cancelled
     * would otherwise leave them showing choices that were never saved.
     */
    openWizard() {
      this.wizardStep = 1;
      this.newUsername = this.status.admin_username || 'admin';
      this.newPassword = '';
      this.newPasswordConfirm = '';
      this.enableSecurityCheckbox = this.status.security_enabled;
      this.enableTlsCheckbox = this.status.tls_enabled;
      this.enableMtlsCheckbox = this.status.mtls_enabled;
      this.tlsCertPath = this.status.tls_cert_path;
      this.tlsKeyPath = this.status.tls_key_path;
      this.clientCaPath = this.status.client_ca_path;
      this.showWizard = true;
    },

    /** Leave the wizard without saving. Discards the working copies above. */
    closeWizard() {
      this.showWizard = false;
      this.wizardStep = 1;
      this.newPassword = '';
      this.newPasswordConfirm = '';
    },

    /** Flip HTTPS on or off without walking the setup wizard.
     *
     * Posts only tls_enabled. Every other field is omitted, and the server
     * treats absent as "leave alone" — so this cannot touch the password, the
     * username, or the authentication switch. That isolation is the point: the
     * wizard route asks for an administrator password on the way past, which is
     * a lot of risk to accept for one checkbox.
     */
    async toggleTls() {
      const target = !this.status.tls_enabled;
      const verb = target ? 'on' : 'off';
      if (!await this.$store.app.confirm({
        title: `Turn HTTPS ${verb}?`,
        body: 'This takes effect when the app restarts, not immediately.',
        detail: target
          ? 'The dashboard will then be reachable over https:// only — a http:// bookmark will stop working.'
          : 'The dashboard will then answer plain http:// — a https:// bookmark will stop working. Sign-in is unaffected; it is a separate switch.',
        confirmLabel: target ? 'Turn HTTPS on' : 'Turn HTTPS off',
        danger: !target,
      })) return;

      this.tlsSaving = true;
      try {
        const r = await fetch('api/security/setup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tls_enabled: target }),
        });
        const data = await r.json();
        if (!r.ok) {
          this.$store.app.addToast(data.detail || 'Could not change the TLS setting.', 'error');
          return;
        }
        // Re-read rather than trusting the local flag: the stored value is the
        // one the entrypoint will act on at the next start.
        await this.loadStatus();
        // The setting is saved but inert until the socket is rebound, so offer
        // the restart here rather than leaving the user to find a terminal —
        // "takes effect on restart" with no way to restart is a dead end.
        this.$store.app.addToast(
          `HTTPS will be ${target ? 'enabled' : 'disabled'} when the app restarts.`,
          'warn',
          {
            detail: target
              ? 'After restarting, use https:// — a http:// bookmark will stop working.'
              : 'After restarting, use http:// — a https:// bookmark will stop working.',
            autoDismiss: false,
            action: { label: 'Restart now', fn: () => this.restartForTls(target) },
          }
        );
      } catch (e) {
        this.$store.app.addToast(`Could not change the TLS setting: ${e.message}`, 'error');
      } finally {
        this.tlsSaving = false;
      }
    },

    // ── MFA ──────────────────────────────────────────────────────────────

    async startMfaEnrolment() {
      this.showMfaModal = true;
      this.mfaLoading = true;
      this.mfaSetup = null;
      this.mfaCode = '';
      try {
        const r = await fetch('api/security/totp/setup', { method: 'POST' });
        const data = await r.json();
        if (!r.ok) {
          this.$store.app.addToast(data.detail || 'Could not start MFA setup.', 'error');
          this.showMfaModal = false;
          return;
        }
        this.mfaSetup = data;
        if (!data.qr_svg) {
          this.$store.app.addToast('QR rendering unavailable — enter the key manually.', 'warn');
        }
      } catch (e) {
        this.$store.app.addToast(`Could not start MFA setup: ${e.message}`, 'error');
        this.showMfaModal = false;
      } finally {
        this.mfaLoading = false;
      }
    },

    async confirmMfaEnrolment() {
      const code = (this.mfaCode || '').trim();
      if (code.length !== 6) {
        this.$store.app.addToast('Enter the 6-digit code from your authenticator.', 'warn');
        return;
      }
      this.mfaLoading = true;
      try {
        const r = await fetch('api/security/totp/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code }),
        });
        const data = await r.json();
        if (!r.ok) {
          // Wrong code is the common case and is not fatal — keep the secret on
          // screen so they can try the next 30-second window without restarting.
          this.$store.app.addToast(data.detail || 'That code was not accepted.', 'error');
          this.mfaCode = '';
          return;
        }
        this.$store.app.addToast('Two-factor authentication is on. You will need a code at next sign-in.', 'info');
        this.closeMfaModal();
        await this.loadUsers();
      } catch (e) {
        this.$store.app.addToast(`Verification failed: ${e.message}`, 'error');
      } finally {
        this.mfaLoading = false;
      }
    },

    closeMfaModal() {
      // The secret is dropped from memory on close. An abandoned enrolment
      // leaves totp_enabled at 0, so the half-written secret is inert.
      this.showMfaModal = false;
      this.mfaSetup = null;
      this.mfaCode = '';
    },

    async disableOwnMfa() {
      if (!await this.$store.app.confirm({
        title: 'Turn off two-factor authentication?',
        body: 'Your account will be protected by its password alone.',
        confirmLabel: 'Turn off', danger: true,
      })) return;
      try {
        const r = await fetch('api/security/totp/disable', { method: 'POST' });
        const data = await r.json();
        if (!r.ok) {
          this.$store.app.addToast(data.detail || 'Could not turn MFA off.', 'error');
          return;
        }
        this.$store.app.addToast('Two-factor authentication turned off.', 'info');
        await this.loadUsers();
      } catch (e) {
        this.$store.app.addToast(`Could not turn MFA off: ${e.message}`, 'error');
      }
    },

    async resetUserMfa(username) {
      if (!await this.$store.app.confirm({
        title: `Clear MFA for ${username}?`,
        body: 'Their authenticator stops working. They sign in with a password '
            + 'alone until they enrol again.',
        confirmLabel: 'Clear MFA', danger: true,
      })) return;
      try {
        const r = await fetch(`api/security/users/${encodeURIComponent(username)}/reset-mfa`, { method: 'POST' });
        const data = await r.json();
        if (!r.ok) {
          this.$store.app.addToast(data.detail || 'Could not clear MFA.', 'error');
          return;
        }
        this.$store.app.addToast(data.message || `MFA cleared for ${username}.`, 'info');
        await this.loadUsers();
      } catch (e) {
        this.$store.app.addToast(`Could not clear MFA: ${e.message}`, 'error');
      }
    },

    openChangePasswordModal(username) {
      this.changePasswordTargetUser = username;
      this.changePasswordNewValue = '';
      this.changePasswordConfirmValue = '';
      this.showChangePasswordModal = true;
    },

    async submitChangePassword() {
      if (this.changePasswordNewValue.length < 8) {
        this.$store.app.addToast('Password must be at least 8 characters long.', 'error');
        return;
      }
      if (this.changePasswordNewValue !== this.changePasswordConfirmValue) {
        this.$store.app.addToast('Passwords do not match.', 'error');
        return;
      }

      this.loading = true;
      try {
        const r = await fetch(`api/security/users/${this.changePasswordTargetUser}/change-password`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: this.changePasswordTargetUser,
            new_password: this.changePasswordNewValue
          })
        });
        const data = await r.json();
        if (r.ok) {
          this.$store.app.addToast(`Password for '${this.changePasswordTargetUser}' reset successfully.`, 'info');
          this.showChangePasswordModal = false;
          await this.loadUsers();
        } else {
          this.$store.app.addToast(data.detail || 'Failed to reset password.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast(`Failed to reset password: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    }
  };
}
