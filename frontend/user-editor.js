/**
 * UserEditor component
 * Modal for creating and editing users
 */

const UserEditor = {
    currentUserId: null,
    availableRoles: [],
    
    /**
     * Open editor modal
     */
    open: function(userId) {
        this.currentUserId = userId;
        this.loadRoles().then(() => {
            this.render();
            this.showModal();
        });
    },
    
    /**
     * Load available roles from API
     */
    loadRoles: function() {
        const self = this;
        return new Promise((resolve, reject) => {
            $.ajax({
                url: `${Auth.getApiUrl()}/api/roles`,
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${Auth.getToken()}`
                },
                success: function(response) {
                    if (response.success && response.roles) {
                        self.availableRoles = response.roles;
                    } else {
                        self.availableRoles = [];
                    }
                    resolve();
                },
                error: function(xhr, status, error) {
                    console.error('[UserEditor] Failed to load roles:', error);
                    self.availableRoles = [];
                    resolve(); // Continue even if roles fail to load
                }
            });
        });
    },
    
    /**
     * Load user data if editing
     */
    loadUser: function() {
        if (!this.currentUserId) {
            return Promise.resolve(null);
        }
        
        const self = this;
        return new Promise((resolve, reject) => {
            $.ajax({
                url: `${Auth.getApiUrl()}/api/users/${this.currentUserId}`,
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${Auth.getToken()}`
                },
                success: function(response) {
                    if (response.success && response.user) {
                        resolve(response.user);
                    } else {
                        reject(new Error('User not found'));
                    }
                },
                error: function(xhr, status, error) {
                    reject(new Error(xhr.responseJSON?.error || error));
                }
            });
        });
    },
    
    /**
     * Render modal
     */
    render: function() {
        const modalId = 'user-editor-modal';
        let modal = document.getElementById(modalId);
        
        if (!modal) {
            modal = document.createElement('div');
            modal.id = modalId;
            modal.className = 'modal';
            modal.style.cssText = 'display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.5);';
            document.body.appendChild(modal);
        }
        
        const isEdit = !!this.currentUserId;
        const title = isEdit ? 'Edit User' : 'Create User';
        
        modal.innerHTML = `
            <div style="position: relative; background: var(--bg-surface); border: 1px solid var(--border-muted); border-radius: var(--border-radius); max-width: 600px; margin: 50px auto; padding: 0; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);">
                <div style="padding: 20px 24px; border-bottom: 1px solid var(--border-muted); display: flex; justify-content: space-between; align-items: center;">
                    <h2 style="margin: 0; color: var(--text-primary); font-size: 18px; font-weight: 600;">${title}</h2>
                    <button onclick="UserEditor.close()" style="background: transparent; border: none; color: var(--text-secondary); cursor: pointer; font-size: 20px; padding: 0; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center;">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                
                <div style="padding: 24px;">
                    <form id="user-editor-form" onsubmit="UserEditor.save(event)">
                        <div style="margin-bottom: 20px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 8px;">
                                Username <span style="color: var(--text-danger, #ef4444);">*</span>
                            </label>
                            <input type="text" 
                                   id="user-username" 
                                   required
                                   minlength="3"
                                   maxlength="50"
                                   style="width: 100%; padding: 10px 12px; background: var(--bg-elevated); border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); font-size: 14px;"
                                   placeholder="Enter username">
                            <div id="user-username-error" style="color: var(--text-danger, #ef4444); font-size: 12px; margin-top: 4px; display: none;"></div>
                        </div>
                        
                        <div style="margin-bottom: 20px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 8px;">
                                Password ${isEdit ? '<span style="color: var(--text-secondary); font-size: 12px;">(leave empty to keep current)</span>' : '<span style="color: var(--text-danger, #ef4444);">*</span>'}
                            </label>
                            <input type="password" 
                                   id="user-password" 
                                   ${isEdit ? '' : 'required'}
                                   minlength="6"
                                   maxlength="128"
                                   style="width: 100%; padding: 10px 12px; background: var(--bg-elevated); border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); font-size: 14px;"
                                   placeholder="Enter password">
                            <div id="user-password-error" style="color: var(--text-danger, #ef4444); font-size: 12px; margin-top: 4px; display: none;"></div>
                        </div>
                        
                        <div style="margin-bottom: 20px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 8px;">
                                Email
                            </label>
                            <input type="email" 
                                   id="user-email" 
                                   maxlength="255"
                                   style="width: 100%; padding: 10px 12px; background: var(--bg-elevated); border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); font-size: 14px;"
                                   placeholder="user@example.com">
                            <div id="user-email-error" style="color: var(--text-danger, #ef4444); font-size: 12px; margin-top: 4px; display: none;"></div>
                        </div>
                        
                        <div style="margin-bottom: 24px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 12px;">
                                Roles
                            </label>
                            <div id="user-roles-container" style="max-height: 200px; overflow-y: auto; border: 1px solid var(--border-muted); border-radius: 6px; padding: 12px; background: var(--bg-elevated);">
                                ${this.availableRoles.length === 0 
                                    ? '<div style="color: var(--text-secondary); font-size: 13px; text-align: center; padding: 20px;">No roles available</div>'
                                    : this.availableRoles.map(role => `
                                        <label style="display: flex; align-items: center; padding: 8px; cursor: pointer; border-radius: 4px; margin-bottom: 4px; transition: background 0.2s;" 
                                               onmouseover="this.style.background='var(--bg-surface)'" 
                                               onmouseout="this.style.background='transparent'">
                                            <input type="checkbox" 
                                                   value="${role.id}" 
                                                   class="user-role-checkbox"
                                                   style="margin-right: 10px; cursor: pointer;">
                                            <div>
                                                <div style="color: var(--text-primary); font-size: 14px; font-weight: 500;">${this.escapeHtml(role.name || '')}</div>
                                                ${role.description ? `<div style="color: var(--text-secondary); font-size: 12px; margin-top: 2px;">${this.escapeHtml(role.description)}</div>` : ''}
                                            </div>
                                        </label>
                                    `).join('')
                                }
                            </div>
                        </div>
                        
                        <div id="user-editor-error" style="color: var(--text-danger, #ef4444); font-size: 13px; margin-bottom: 16px; display: none;"></div>
                        
                        <div style="display: flex; gap: 12px; justify-content: flex-end;">
                            <button type="button" 
                                    onclick="UserEditor.close()"
                                    style="padding: 10px 20px; background: transparent; border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); cursor: pointer; font-size: 14px; font-weight: 500;">
                                Cancel
                            </button>
                            <button type="submit"
                                    style="padding: 10px 20px; background: var(--accent-primary); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
                                ${isEdit ? 'Update' : 'Create'}
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        `;
        
        // Load user data if editing
        if (isEdit) {
            this.loadUser().then(user => {
                document.getElementById('user-username').value = user.username || '';
                document.getElementById('user-email').value = user.email || '';
                
                // Check roles
                if (user.roles && Array.isArray(user.roles)) {
                    user.roles.forEach(roleId => {
                        const checkbox = document.querySelector(`.user-role-checkbox[value="${roleId}"]`);
                        if (checkbox) {
                            checkbox.checked = true;
                        }
                    });
                }
            }).catch(error => {
                this.showError(error.message);
            });
        }
    },
    
    /**
     * Show modal
     */
    showModal: function() {
        const modal = document.getElementById('user-editor-modal');
        if (modal) {
            modal.style.display = 'block';
        }
    },
    
    /**
     * Close modal
     */
    close: function() {
        const modal = document.getElementById('user-editor-modal');
        if (modal) {
            modal.style.display = 'none';
        }
        this.currentUserId = null;
    },
    
    /**
     * Save user
     */
    save: function(event) {
        event.preventDefault();
        
        const username = document.getElementById('user-username').value.trim();
        const password = document.getElementById('user-password').value;
        const email = document.getElementById('user-email').value.trim();
        
        // Get selected roles
        const selectedRoles = Array.from(document.querySelectorAll('.user-role-checkbox:checked'))
            .map(cb => cb.value);
        
        // Validation
        if (!username || username.length < 3) {
            this.showFieldError('user-username', 'Username must be at least 3 characters');
            return;
        }
        
        // Validate username format (alphanumeric, underscore, hyphen only)
        const usernamePattern = /^[a-zA-Z0-9_-]+$/;
        if (!usernamePattern.test(username)) {
            this.showFieldError('user-username', 'Username can only contain letters, numbers, underscores, and hyphens');
            return;
        }
        
        if (!this.currentUserId && !password) {
            this.showFieldError('user-password', 'Password is required');
            return;
        }
        
        if (password && password.length < 6) {
            this.showFieldError('user-password', 'Password must be at least 6 characters');
            return;
        }
        
        if (email && !this.validateEmail(email)) {
            this.showFieldError('user-email', 'Invalid email format');
            return;
        }
        
        // Clear errors
        this.clearErrors();
        
        const self = this;
        const url = this.currentUserId 
            ? `${Auth.getApiUrl()}/api/users/${this.currentUserId}`
            : `${Auth.getApiUrl()}/api/users`;
        const method = this.currentUserId ? 'PUT' : 'POST';
        
        const data = {
            username: username,
            email: email || null,
            roles: selectedRoles
        };
        
        if (password) {
            data.password = password;
        }
        
        $.ajax({
            url: url,
            method: method,
            contentType: 'application/json',
            headers: {
                'Authorization': `Bearer ${Auth.getToken()}`
            },
            data: JSON.stringify(data),
            success: function(response) {
                if (response.success) {
                    self.showSuccess(this.currentUserId ? 'User updated successfully' : 'User created successfully');
                    self.close();
                    if (typeof UsersTable !== 'undefined') {
                        UsersTable.loadUsers();
                    }
                } else {
                    self.showError(response.error || 'Failed to save user');
                }
            },
            error: function(xhr, status, error) {
                const errorMsg = xhr.responseJSON?.error || error;
                self.showError('Failed to save user: ' + errorMsg);
                
                // Show field-specific errors if available
                if (xhr.responseJSON && xhr.responseJSON.errors) {
                    Object.keys(xhr.responseJSON.errors).forEach(field => {
                        self.showFieldError(`user-${field}`, xhr.responseJSON.errors[field]);
                    });
                }
            }
        });
    },
    
    /**
     * Show field error
     */
    showFieldError: function(fieldId, message) {
        const errorEl = document.getElementById(fieldId + '-error');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.style.display = 'block';
        }
    },
    
    /**
     * Clear all errors
     */
    clearErrors: function() {
        document.querySelectorAll('[id$="-error"]').forEach(el => {
            el.style.display = 'none';
        });
    },
    
    /**
     * Show error message
     */
    showError: function(message) {
        const errorEl = document.getElementById('user-editor-error');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.style.display = 'block';
        }
    },
    
    /**
     * Show success message
     */
    showSuccess: function(message) {
        if (typeof Toast !== 'undefined' && Toast.success) {
            Toast.success(message);
        } else {
            alert(message);
        }
    },
    
    /**
     * Validate email
     */
    validateEmail: function(email) {
        const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        return re.test(email);
    },
    
    /**
     * Escape HTML
     */
    escapeHtml: function(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};

// Закрытие по клику на overlay — как в других модалках: не закрывать, если выделяли текст
(function() {
    let mouseDownInsideContent = false;
    document.addEventListener('mousedown', function(e) {
        const modal = document.getElementById('user-editor-modal');
        if (!modal || modal.style.display !== 'block') return;
        const content = modal.querySelector(':scope > div');
        mouseDownInsideContent = content ? content.contains(e.target) : (modal.contains(e.target) && e.target !== modal);
    });
    document.addEventListener('click', function(e) {
        const modal = document.getElementById('user-editor-modal');
        if (!modal || modal.style.display !== 'block') return;
        if (e.target === modal && !mouseDownInsideContent) {
            UserEditor.close();
        }
        mouseDownInsideContent = false;
    });
})();
