define(['emby-input', 'emby-button', 'emby-select'], function () {
    'use strict';

    var pluginId = '3d9e8cda-9217-4ca9-9353-8c89adda7789';

    function selectedValues(select) {
        return Array.from(select.selectedOptions).map(function (option) {
            return option.value;
        });
    }

    function setSelectedValues(select, values) {
        var selected = new Set((values || []).map(String));
        Array.from(select.options).forEach(function (option) {
            option.selected = selected.has(String(option.value));
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
                (users || []).forEach(function (user) {
                    var option = document.createElement('option');
                    option.value = user.Id;
                    option.textContent = user.Name || user.Id;
                    select.appendChild(option);
                });
            });
            setSelectedValues(view.querySelector('#mw-movie-users'), config.MovieHomeSectionUserIds);
            setSelectedValues(view.querySelector('#mw-tv-users'), config.TvHomeSectionUserIds);
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
            return loadUsers(view, config);
        }).catch(function (error) {
            view.querySelector('#mw-config-error').textContent =
                'Could not load plugin settings: ' + (error.message || error);
        });
    }

    function saveConfiguration(view) {
        var button = view.querySelector('#mw-save');
        var error = view.querySelector('#mw-config-error');
        var status = view.querySelector('#mw-config-status');
        button.disabled = true;
        error.textContent = '';
        status.textContent = '';

        window.ApiClient.getPluginConfiguration(pluginId).then(function (config) {
            config = config || {};
            config.MovieHomeSectionUserIds = selectedValues(view.querySelector('#mw-movie-users'));
            config.TvHomeSectionUserIds = selectedValues(view.querySelector('#mw-tv-users'));
            config.MovieHomeSectionImageType = view.querySelector('#mw-movie-image').value;
            config.TvHomeSectionImageType = view.querySelector('#mw-tv-image').value;
            config.MovieHomeSectionSortBy = view.querySelector('#mw-movie-sort').value;
            config.TvHomeSectionSortBy = view.querySelector('#mw-tv-sort').value;
            config.MovieHomeSectionSortOrder = view.querySelector('#mw-movie-order').value;
            config.TvHomeSectionSortOrder = view.querySelector('#mw-tv-order').value;
            return window.ApiClient.updatePluginConfiguration(pluginId, config);
        }).then(function () {
            status.textContent = 'Saved. HomeScreen sections will be synchronized by the bridge.';
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
