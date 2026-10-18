define(['emby-input', 'emby-button', 'emby-select'], function () {
    'use strict';

    var pluginId = '3d9e8cda-9217-4ca9-9353-8c89adda7789';

    function selectedValues(select) {
        var selected = typeof select.getValues === 'function'
            ? select.getValues()
            : [];
        if (!Array.isArray(selected) || selected.length === 0) {
            selected = Array.from(select.options).filter(function (option) {
                return option.selected;
            }).map(function (option) {
                return option.value;
            });
        }
        selected = selected.map(String);
        return selected.indexOf('*') >= 0 ? ['*'] : selected;
    }

    function sameValues(left, right) {
        var a = (left || []).map(String).sort();
        var b = (right || []).map(String).sort();
        return a.length === b.length && a.every(function (value, index) {
            return value === b[index];
        });
    }

    function setSelectedValues(select, values) {
        var selected = (values || []).map(String);
        if (typeof select.setValues === 'function') {
            select.setValues(selected, false);
            return;
        }
        var selectedSet = new Set(selected);
        Array.from(select.options).forEach(function (option) {
            option.selected = selectedSet.has(String(option.value));
        });
    }

    function requestHeaders() {
        var token = window.ApiClient && window.ApiClient.accessToken
            ? window.ApiClient.accessToken()
            : '';
        return token ? {'X-Emby-Token': token} : {};
    }

    function loadUsers(view, config) {
        var url = window.ApiClient.getUrl('Users');
        return fetch(url, {headers: requestHeaders()}).then(function (response) {
            if (!response.ok) throw new Error('Emby returned HTTP ' + response.status + ' loading users.');
            return response.json();
        }).then(function (users) {
            ['mw-movie-users', 'mw-tv-users'].forEach(function (id) {
                var select = view.querySelector('#' + id);
                select.innerHTML = '';
                var allOption = document.createElement('option');
                allOption.value = '*';
                allOption.textContent = 'All users';
                select.appendChild(allOption);
                (users || []).forEach(function (user) {
                    var option = document.createElement('option');
                    option.value = user.Id;
                    option.textContent = user.Name || user.Id;
                    select.appendChild(option);
                });
                select.onchange = function () {
                    var selected = selectedValues(select);
                    if (selected.indexOf('*') >= 0 && selected.length > 1)
                        setSelectedValues(select, ['*']);
                };
            });
            setSelectedValues(view.querySelector('#mw-movie-users'), config.MovieHomeSectionUserIds);
            setSelectedValues(view.querySelector('#mw-tv-users'), config.TvHomeSectionUserIds);
        });
    }

    function loadLibraries(view, config) {
        var url = window.ApiClient.getUrl('Library/VirtualFolders');
        return fetch(url, {headers: requestHeaders()}).then(function (response) {
            if (!response.ok) throw new Error('Emby returned HTTP ' + response.status + ' loading libraries.');
            return response.json();
        }).then(function (libraries) {
            [
                {id: 'mw-movie-library', type: 'movies', selected: config.MovieHomeSectionLibraryId},
                {id: 'mw-tv-library', type: 'tvshows', selected: config.TvHomeSectionLibraryId}
            ].forEach(function (spec) {
                var select = view.querySelector('#' + spec.id);
                select.innerHTML = '<option value="">Auto-detect Manager target library</option>';
                (libraries || []).filter(function (library) {
                    return String(library.CollectionType || '').toLowerCase() === spec.type;
                }).forEach(function (library) {
                    var option = document.createElement('option');
                    option.value = library.ItemId || library.Id;
                    option.textContent = library.Name || option.value;
                    select.appendChild(option);
                });
                select.value = spec.selected || '';
            });
        });
    }

    function loadConfiguration(view) {
        view.querySelector('#mw-config-error').textContent = '';
        return window.ApiClient.getPluginConfiguration(pluginId).then(function (config) {
            config = config || {};
            view.querySelector('#mw-movie-image').value = config.MovieHomeSectionImageType || 'Primary';
            view.querySelector('#mw-tv-image').value = config.TvHomeSectionImageType || 'Primary';
            view.querySelector('#mw-movie-sort').value = config.MovieHomeSectionSortBy || 'SortName';
            view.querySelector('#mw-tv-sort').value = config.TvHomeSectionSortBy || 'SortName';
            view.querySelector('#mw-movie-order').value = config.MovieHomeSectionSortOrder || 'Ascending';
            view.querySelector('#mw-tv-order').value = config.TvHomeSectionSortOrder || 'Ascending';
            view.querySelector('#mw-movie-detected').textContent = config.ActiveMovieOutputLibrary
                ? 'Detected active Manager output: ' + config.ActiveMovieOutputLibrary
                : 'No active Manager Movies output manifest detected.';
            view.querySelector('#mw-tv-detected').textContent = config.ActiveTvOutputLibrary
                ? 'Detected active Manager output: ' + config.ActiveTvOutputLibrary
                : 'No active Manager TV output manifest detected.';
            view.querySelector('#mw-movie-status').textContent = config.MovieHomeSectionStatus || '';
            view.querySelector('#mw-tv-status').textContent = config.TvHomeSectionStatus || '';
            return Promise.all([loadUsers(view, config), loadLibraries(view, config)]);
        }).catch(function (error) {
            view.querySelector('#mw-config-error').textContent =
                'Could not load plugin settings: ' + (error.message || error);
        });
    }

    function saveConfiguration(view) {
        var button = view.querySelector('#mw-save');
        var error = view.querySelector('#mw-config-error');
        var status = view.querySelector('#mw-config-status');
        var movieUserIds = selectedValues(view.querySelector('#mw-movie-users'));
        var tvUserIds = selectedValues(view.querySelector('#mw-tv-users'));
        button.disabled = true;
        error.textContent = '';
        status.textContent = 'Saving HomeScreen users: Movies ' + movieUserIds.length +
            ', TV Shows ' + tvUserIds.length + '.';
        console.info('[Emby Collection Manager] Saving HomeScreen user selections', {
            MovieHomeSectionUserIds: movieUserIds,
            TvHomeSectionUserIds: tvUserIds
        });

        window.ApiClient.getPluginConfiguration(pluginId).then(function (config) {
            config = config || {};
            config.MovieHomeSectionLibraryId = view.querySelector('#mw-movie-library').value;
            config.TvHomeSectionLibraryId = view.querySelector('#mw-tv-library').value;
            config.MovieHomeSectionUserIds = movieUserIds;
            config.TvHomeSectionUserIds = tvUserIds;
            config.MovieHomeSectionImageType = view.querySelector('#mw-movie-image').value;
            config.TvHomeSectionImageType = view.querySelector('#mw-tv-image').value;
            config.MovieHomeSectionSortBy = view.querySelector('#mw-movie-sort').value;
            config.TvHomeSectionSortBy = view.querySelector('#mw-tv-sort').value;
            config.MovieHomeSectionSortOrder = view.querySelector('#mw-movie-order').value;
            config.TvHomeSectionSortOrder = view.querySelector('#mw-tv-order').value;
            return window.ApiClient.updatePluginConfiguration(pluginId, config).then(function (response) {
                console.debug('[Emby Collection Manager] Plugin configuration update response', response);
                return window.ApiClient.getPluginConfiguration(pluginId);
            });
        }).then(function (saved) {
            if (!sameValues(saved.MovieHomeSectionUserIds, movieUserIds) ||
                !sameValues(saved.TvHomeSectionUserIds, tvUserIds)) {
                throw new Error('Emby accepted the request but did not return the selected HomeScreen user IDs. Check the plugin configuration file and browser request payload.');
            }
            status.textContent = 'Saved and verified. The bridge is synchronizing the selected HomeScreen sections.';
        }).catch(function (saveError) {
            error.textContent = 'Could not save plugin settings: ' + (saveError.message || saveError);
        }).finally(function () {
            button.disabled = false;
        });
    }

    return function (view) {
        view.addEventListener('viewshow', function () {
            loadConfiguration(view);
        });
        view.querySelector('#mw-save').addEventListener('click', function () {
            saveConfiguration(view);
        });
    };
});
