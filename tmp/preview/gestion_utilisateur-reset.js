
        (function() {
            
            window.switchUserFormTab = function(key) {
                document.querySelectorAll('.user-form-panel').forEach(panel => {
                    panel.classList.toggle('hidden', panel.id !== 'user-panel-' + key);
                });
                document.querySelectorAll('.user-form-tab').forEach(button => {
                    const selected = button.id === 'user-tab-' + key;
                    button.setAttribute('aria-selected', String(selected));
                    button.classList.toggle('bg-brand-neon', selected);
                    button.classList.toggle('text-black', selected);
                    button.classList.toggle('text-slate-500', !selected);
                });
            };
            document.getElementById('userForm').addEventListener('invalid', event => {
                const panel = event.target.closest('.user-form-panel');
                if (panel) switchUserFormTab(panel.id.replace('user-panel-', ''));
            }, true);
            switchUserFormTab('general');
    let usersData = [];
            let availableSpecialites = [];
            let availableRdvSchedules = [];
            let filteredUsersList = [];
            let currentUserPage = 1;
            const USERS_PER_PAGE = 15;

            const USER_ROLE_PRESETS = {
                "Médecin": [
                    "dash_view", "reception_view", "pat_view", "pat_create", "pat_edit", 
                    "dme_view", "dme_consult", "dme_prescribe", "dme_demandes",
                    "act_view", "ctype_view", "cr_view", "med_view", "room_manage", "rdv_view", "rdv_manage"
                ],
                "Réceptionniste": [
                    "dash_view", "reception_view", "reception_create", "reception_edit",
                    "cash_collect", "cash_collect_rest", "pat_view", "pat_create", "pat_edit", "act_view", "ctype_view", "med_view", "rdv_view", "rdv_manage"
                ],
                "Caissier": [
                    "dash_view", "reception_view", "cash_collect", "cash_collect_rest", "cash_refund", "cash_remise", "cash_reports",
                    "pat_view", "act_view", "ctype_view"
                ],
                "Surveillant Médical": [
                    "dash_view", "reception_view", "pat_view", "dme_view", "act_view", "med_view", "room_manage", "rdv_view"
                ],
                "Direction": [
                    "dash_view", "dash_financial", "reception_view", "cash_reports", "cash_remise", "cash_refund",
                    "pat_view", "dme_view", "act_view", "ctype_view", "med_view", "doc_manage", "room_manage", "clinic_settings", "admin_users", "rdv_view"
                ],
                "Administrateur": [
                    "dash_view", "dash_financial", "reception_view", "reception_create", "reception_edit",
                    "cash_collect", "cash_collect_rest", "cash_refund", "cash_remise", "cash_reports", "pat_view", "pat_create", "pat_edit", "pat_delete",
                    "dme_view", "dme_consult", "dme_reset", "dme_prescribe", "dme_demandes",
                    "act_view", "act_manage", "ctype_view", "ctype_manage", "cr_view", "cr_manage",
                    "med_view", "med_manage", "med_import_export", "doc_manage", "room_manage", "clinic_settings",
                    "admin_users", "admin_perms", "rdv_view", "rdv_manage"
                ]
            };

            const roleBadges = {
                'Médecin': 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800/40',
                'Réceptionniste': 'bg-sky-50 dark:bg-sky-950/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                'Caissier': 'bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-800/40',
                'Surveillant Médical': 'bg-purple-50 dark:bg-purple-950/40 text-purple-700 dark:text-purple-300 border-purple-200 dark:border-purple-800/40',
                'Direction': 'bg-slate-800 text-white border-slate-700',
                'Administrateur': 'bg-brand-neon/20 text-emerald-900 dark:text-brand-neon border-brand-neon/30'
            };

            window.loadUsersModule = async function() {
                try {
                    const res = await fetch('/api/users');
                    const data = await res.json();
                    usersData = data.users || [];
                    availableSpecialites = data.available_specialites || [];
                    availableRdvSchedules = data.available_rdv_schedules || [];

                    document.getElementById('totalUsersCount').innerText = data.total_count || usersData.length;
                    renderSpecialitesCheckboxes();
                    renderRdvScheduleCheckboxes();
                    filterUsers();
                } catch(err) {
                    console.error("Erreur chargement utilisateurs:", err);
                }
            };

            // Rendu dynamique des cases à cocher des spécialités
            function renderSpecialitesCheckboxes() {
                const grid = document.getElementById('userSpecialitesGrid');
                if (!grid) return;
                grid.innerHTML = '';

                if (availableSpecialites.length === 0) {
                    grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-3 py-2">Aucune spécialité trouvée en base. Ajoutez-en dans Gestion Spécialités.</span>';
                    return;
                }

                availableSpecialites.forEach(s => {
                    const label = document.createElement('label');
                    label.className = "flex items-center gap-2.5 p-2 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer transition";
                    label.innerHTML = `
                        <input type="checkbox" value="${s.nom}" class="user-specialite-check rounded text-brand-neon focus:ring-brand-neon">
                        <div class="truncate">
                            <span class="text-xs font-semibold text-slate-700 dark:text-slate-300 block truncate">${s.nom}</span>
                            ${s.code ? `<span class="text-[10px] text-slate-400 font-mono">${s.code}</span>` : ''}
                        </div>
                    `;
                    grid.appendChild(label);
                });
            }

            function renderRdvScheduleCheckboxes() {
                const grid = document.getElementById('userRdvSchedulesGrid');
                if (!grid) return;
                grid.innerHTML = '';
                if (!availableRdvSchedules.length) {
                    grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-2 py-2">Aucun planning enregistré. Créez d’abord un planning dans Rendez-vous.</span>';
                    return;
                }
                availableRdvSchedules.forEach(schedule => {
                    const days = (schedule.jours_semaine || [schedule.jour_semaine]).map(Number)
                        .map(day => ['Lun','Mar','Mer','Jeu','Ven','Sam','Dim'][day]).join(', ');
                    const label = document.createElement('label');
                    label.className = 'flex items-start gap-2.5 p-2.5 rounded-xl border border-indigo-100 dark:border-white/10 bg-white dark:bg-[#121c15] hover:bg-indigo-50/60 dark:hover:bg-white/5 cursor-pointer transition';
                    label.innerHTML = `<input type="checkbox" value="${Number(schedule.id)}" class="user-rdv-schedule-check mt-0.5 rounded text-brand-neon focus:ring-brand-neon"><span class="min-w-0"><b class="block truncate text-xs text-slate-700 dark:text-slate-200">${schedule.specialite}</b><small class="block text-[10px] text-slate-500">${days} · ${String(schedule.heure_debut).slice(0,5)}–${String(schedule.heure_fin).slice(0,5)}</small></span>`;
                    grid.appendChild(label);
                });
            }

            window.toggleAllRdvSchedules = function(check) {
                document.querySelectorAll('.user-rdv-schedule-check').forEach(cb => cb.checked = check);
            };

            window.toggleAllSpecialites = function(check) {
                document.querySelectorAll('.user-specialite-check').forEach(cb => cb.checked = check);
            };

            window.filterUsers = function() {
                const q = (document.getElementById('searchUserInput')?.value || '').toLowerCase().trim();
                if (!q) {
                    filteredUsersList = usersData;
                } else {
                    filteredUsersList = usersData.filter(u => 
                        (u.nom && u.nom.toLowerCase().includes(q)) || 
                        (u.prenom && u.prenom.toLowerCase().includes(q)) || 
                        (u.role && u.role.toLowerCase().includes(q)) || 
                        (u.telephone && u.telephone.includes(q)) || 
                        (u.login && u.login.toLowerCase().includes(q)) ||
                        (u.specialites_autorisees && u.specialites_autorisees.some(s => s.toLowerCase().includes(q)))
                    );
                }
                currentUserPage = 1;
                renderUsersTable();
            };

            window.renderUsersTable = function() {
                const tbody = document.getElementById('users-table-body');
                tbody.innerHTML = '';

                const totalItems = filteredUsersList.length;
                const totalPages = Math.ceil(totalItems / USERS_PER_PAGE) || 1;

                if (currentUserPage > totalPages) currentUserPage = totalPages;
                if (currentUserPage < 1) currentUserPage = 1;

                const startIndex = (currentUserPage - 1) * USERS_PER_PAGE;
                const endIndex = startIndex + USERS_PER_PAGE;
                const pageItems = filteredUsersList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-slate-400">Aucun utilisateur trouvé.</td></tr>';
                } else {
                    pageItems.forEach(u => {
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                        const activePermsCount = Object.values(u.permissions || {}).filter(Boolean).length;
                        const statusBadge = u.status === 'Actif' 
                            ? `<span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-emerald-100 dark:bg-emerald-950/60 text-emerald-800 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800/40 cursor-pointer" title="Cliquer pour désactiver">🟢 Actif</span>`
                            : `<span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-rose-100 dark:bg-rose-950/60 text-rose-800 dark:text-rose-300 border border-rose-300 dark:border-rose-800/40 cursor-pointer" title="Cliquer pour activer">🔴 Inactif</span>`;

                        // Badge du périmètre médical
                        const specs = u.specialites_autorisees || [];
                        let specsBadge = '';
                        if (availableSpecialites.length > 0 && specs.length >= availableSpecialites.length) {
                            specsBadge = `<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30" title="Accès illimité à toutes les spécialités">🌐 Toutes les Spécialités</span>`;
                        } else if (specs.length === 0) {
                            specsBadge = `<span class="text-slate-400 text-[11px] italic">Aucune spécialité</span>`;
                        } else {
                            const tooltip = specs.join(', ');
                            specsBadge = `<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border border-sky-200 dark:border-sky-800/40 cursor-help" title="${tooltip}">🩺 ${specs.length} autorisée(s)</span>`;
                        }

                        tr.innerHTML = `
                            <td class="py-3.5 pl-2 flex items-center space-x-3">
                                <img src="${u.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80'}" class="w-9 h-9 rounded-xl object-cover shadow-sm border border-slate-200 dark:border-white/10">
                                <div>
                                    <div class="font-bold text-slate-800 dark:text-slate-100 text-xs">${u.prenom} ${u.nom}</div>
                                    <div class="text-[10px] text-slate-400 font-mono">ID: #${u.id}</div>
                                </div>
                            </td>
                            <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold border ${roleBadges[u.role] || 'bg-slate-100'}">${u.role}</span></td>
                            <td class="py-3.5">${specsBadge}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-300 font-medium">${u.telephone}</td>
                            <td class="py-3.5 text-slate-700 dark:text-slate-200 font-mono text-[11px] font-bold">${u.login}</td>
                            <td class="py-3.5" onclick="toggleUserStatus(${u.id}, '${u.status}')">${statusBadge}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-400 font-semibold text-xs">
                                <span class="bg-slate-100 dark:bg-black/40 px-2 py-0.5 rounded-lg border border-slate-200 dark:border-white/10">${activePermsCount} droits</span>
                            </td>
                            <td class="py-3.5 pr-2 text-right">
                                <div class="inline-flex items-center gap-1.5">
                                    <button type="button" onclick="editUser(${u.id})" title="Modifier permissions & infos" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg transition"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button type="button" onclick="deleteUser(${u.id})" title="Supprimer utilisateur" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg transition"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                }

                // GESTION DE LA BARRE DE PAGINATION
                const paginationWrapper = document.getElementById('userPaginationWrapper');
                const info = document.getElementById('userPaginationInfo');
                const controls = document.getElementById('userPaginationControls');

                if (totalItems > 0) {
                    paginationWrapper.classList.remove('hidden');
                    info.innerHTML = `Affichage de <strong>${startIndex + 1}</strong> à <strong>${Math.min(endIndex, totalItems)}</strong> sur <strong>${totalItems}</strong> collaborateurs`;

                    if (totalItems > USERS_PER_PAGE) {
                        let html = '';
                        const prevDisabled = currentUserPage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setUserPage(${currentUserPage - 1})" ${currentUserPage <= 1 ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${prevDisabled}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {
                            if (i === 1 || i === totalPages || (i >= currentUserPage - 1 && i <= currentUserPage + 1)) {
                                const activeClass = i === currentUserPage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setUserPage(${i})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${activeClass}">${i}</button>`;
                            } else if (i === currentUserPage - 2 || i === currentUserPage + 2) {
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }
                        }

                        const nextDisabled = currentUserPage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setUserPage(${currentUserPage + 1})" ${currentUserPage >= totalPages ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${nextDisabled}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    } else {
                        controls.innerHTML = '';
                    }
                } else {
                    paginationWrapper.classList.add('hidden');
                }
            };

            window.setUserPage = function(p) {
                currentUserPage = p;
                renderUsersTable();
            };

            window.previewUserAvatar = function(event) {
                const file = event.target.files[0];
                if (file) {
                    const reader = new FileReader();
                    reader.onload = function(e) {
                        document.getElementById('avatarPreview').src = e.target.result;
                        document.getElementById('avatarData').value = e.target.result;
                    };
                    reader.readAsDataURL(file);
                }
            };

            window.openUserModal = function() {
                const form = document.getElementById('userForm');
                if (form) form.reset();

                document.getElementById('userId').value = '';
                document.getElementById('avatarData').value = '';
                document.getElementById('avatarPreview').src = '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80';
                document.getElementById('modalTitle').innerText = 'Ajouter un Collaborateur';
                switchUserFormTab('general');

                toggleAllUserPerms(false);
                toggleAllSpecialites(false);
                toggleAllRdvSchedules(false);
                applyUserRolePreset('Médecin');

                document.getElementById('userModal').classList.remove('hidden');
            };

            window.closeUserModal = function() {
                document.getElementById('userModal').classList.add('hidden');
            };

            window.toggleAllUserPerms = function(check) {
                document.querySelectorAll('.user-perm-check').forEach(cb => cb.checked = check);
            };

            window.applyUserRolePreset = function(role) {
                toggleAllUserPerms(false);
                const activeList = USER_ROLE_PRESETS[role] || [];
                activeList.forEach(perm => {
                    const el = document.getElementById('perm_' + perm);
                    if (el) el.checked = true;
                });

                // Si Administrateur ou Direction, on autorise toutes les spécialités par défaut
                if (role === 'Administrateur' || role === 'Direction') {
                    toggleAllSpecialites(true);
                }
            };

            window.editUser = function(userId) {
                const u = usersData.find(x => x.id === userId);
                if (!u) return;

                openUserModal();

                document.getElementById('userId').value = u.id;
                document.getElementById('userPrenom').value = u.prenom || '';
                document.getElementById('userNom').value = u.nom || '';
                document.getElementById('userTelephone').value = u.telephone !== '-' ? u.telephone : '';
                document.getElementById('userRoleSelect').value = u.role || 'Médecin';
                document.getElementById('userLogin').value = u.login || '';
                document.getElementById('userPassword').value = '';
                document.getElementById('avatarData').value = u.avatar || '';
                document.getElementById('avatarPreview').src = u.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80';
                document.getElementById('userStatus').value = u.status || 'Actif';
                document.getElementById('modalTitle').innerText = 'Modifier les droits de ' + u.prenom + ' ' + u.nom;

                document.querySelectorAll('.user-dme-display-check').forEach(cb => { cb.checked = u.permissions?.[cb.id.replace('perm_', '')] !== false; });

                // Re-cochage des permissions
                toggleAllUserPerms(false);
                for (const [permKey, val] of Object.entries(u.permissions || {})) {
                    const el = document.getElementById('perm_' + permKey);
                    if (el) el.checked = Boolean(val);
                }
                if (!Object.prototype.hasOwnProperty.call(u.permissions || {}, 'cash_collect_rest')) {
                    const restPermission = document.getElementById('perm_cash_collect_rest');
                    if (restPermission) restPermission.checked = Boolean(u.permissions?.cash_collect);
                }
                if (!Object.prototype.hasOwnProperty.call(u.permissions || {}, 'cash_refund')) {
                    const refundPermission = document.getElementById('perm_cash_refund');
                    if (refundPermission) refundPermission.checked = Boolean(u.permissions?.cash_remise);
                }

                // Re-cochage des spécialités autorisées
                toggleAllSpecialites(false);
                const userSpecs = u.specialites_autorisees || [];
                document.querySelectorAll('.user-specialite-check').forEach(cb => {
                    cb.checked = userSpecs.includes(cb.value);
                });
                const userSchedules = Array.isArray(u.plannings_rdv_autorises)
                    ? u.plannings_rdv_autorises.map(Number)
                    : availableRdvSchedules.map(schedule => Number(schedule.id));
                document.querySelectorAll('.user-rdv-schedule-check').forEach(cb => {
                    cb.checked = userSchedules.includes(Number(cb.value));
                });
            };

            window.handleUserFormSubmit = async function(e) {
                e.preventDefault();

                // Collecte des permissions granulaires
                const perms = {};
                document.querySelectorAll('.user-perm-check, .user-dme-display-check').forEach(cb => {
                    const key = cb.id.replace('perm_', '');
                    perms[key] = cb.checked;
                });

                // Collecte des spécialités autorisées
                const selectedSpecialites = [];
                document.querySelectorAll('.user-specialite-check:checked').forEach(cb => {
                    selectedSpecialites.push(cb.value);
                });
                const selectedRdvSchedules = [...document.querySelectorAll('.user-rdv-schedule-check:checked')]
                    .map(cb => Number(cb.value));

                const userData = {
                    id: document.getElementById('userId').value ? parseInt(document.getElementById('userId').value) : null,
                    prenom: document.getElementById('userPrenom').value.trim(),
                    nom: document.getElementById('userNom').value.trim(),
                    telephone: document.getElementById('userTelephone').value.trim(),
                    role: document.getElementById('userRoleSelect').value,
                    login: document.getElementById('userLogin').value.trim(),
                    password: document.getElementById('userPassword').value.trim(),
                    avatar: document.getElementById('avatarData').value || document.getElementById('avatarPreview').src,
                    status: document.getElementById('userStatus').value,
                    permissions: perms,
                    specialites_autorisees: selectedSpecialites,
                    plannings_rdv_autorises: selectedRdvSchedules
                };

                const res = await fetch('/api/users/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(userData)
                }).then(r => r.json());

                if (res.success) {
                    closeUserModal();
                    await loadUsersModule();
                    alert(res.message);
                } else {
                    alert(res.message);
                }
            };

            window.toggleUserStatus = async function(id, currentStatus) {
                await fetch('/api/users/toggle-status', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: id, status: currentStatus })
                });
                loadUsersModule();
            };

            window.deleteUser = async function(id) {
                if (confirm("Voulez-vous vraiment supprimer cet utilisateur ?")) {
                    const res = await fetch('/api/users/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: id })
                    }).then(r => r.json());

                    if (res.success) {
                        loadUsersModule();
                    } else {
                        alert(res.message || "Erreur lors de la suppression.");
                    }
                }
            };

            // Chargement initial automatique
            loadUsersModule();
        })();
    