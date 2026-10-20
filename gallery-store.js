(function () {
  const databaseName = "tsc-browser-gallery";
  const storeName = "images";
  const settingsDatabaseName = "tsc-browser-settings";
  const settingsStoreName = "settings";
  let databasePromise;
  let settingsDatabasePromise;

  function openDatabase() {
    if (!("indexedDB" in window)) {
      return Promise.reject(new Error("This browser does not support persistent gallery storage."));
    }
    if (!databasePromise) {
      databasePromise = new Promise((resolve, reject) => {
        const request = indexedDB.open(databaseName, 1);
        request.onupgradeneeded = () => {
          const database = request.result;
          if (!database.objectStoreNames.contains(storeName)) {
            database.createObjectStore(storeName, { keyPath: "id", autoIncrement: true });
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error("Could not open gallery storage."));
        request.onblocked = () => reject(new Error("Gallery storage is blocked by another open TSC page. Close duplicate pages and try again."));
      }).catch(error => {
        databasePromise = undefined;
        throw error;
      });
    }
    return databasePromise;
  }

  function openSettingsDatabase() {
    if (!("indexedDB" in window)) {
      return Promise.reject(new Error("This browser does not support persistent TSC settings storage."));
    }
    if (!settingsDatabasePromise) {
      settingsDatabasePromise = new Promise((resolve, reject) => {
        const request = indexedDB.open(settingsDatabaseName, 1);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains(settingsStoreName)) {
            request.result.createObjectStore(settingsStoreName, { keyPath: "key" });
          }
        };
        request.onsuccess = () => {
          const database = request.result;
          database.onversionchange = () => {
            database.close();
            settingsDatabasePromise = undefined;
          };
          resolve(database);
        };
        request.onerror = () => reject(request.error || new Error("Could not open TSC settings storage."));
        request.onblocked = () => reject(new Error("TSC settings storage is blocked by another open page."));
      }).catch(error => {
        settingsDatabasePromise = undefined;
        throw error;
      });
    }
    return settingsDatabasePromise;
  }

  function runTransaction(mode, operation) {
    return openDatabase().then(database => new Promise((resolve, reject) => {
      const transaction = database.transaction(storeName, mode);
      const request = operation(transaction.objectStore(storeName));
      let result;
      request.onsuccess = () => { result = request.result; };
      request.onerror = () => reject(request.error || new Error("Gallery storage request failed."));
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error || new Error("Gallery storage transaction failed."));
      transaction.onabort = () => reject(transaction.error || new Error("Gallery storage transaction was aborted."));
    }));
  }

  function runSettingsTransaction(mode, operation) {
    return openSettingsDatabase().then(database => new Promise((resolve, reject) => {
      const transaction = database.transaction(settingsStoreName, mode);
      const request = operation(transaction.objectStore(settingsStoreName));
      let result;
      request.onsuccess = () => { result = request.result; };
      request.onerror = () => reject(request.error || new Error("TSC settings storage request failed."));
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error || new Error("TSC settings storage transaction failed."));
      transaction.onabort = () => reject(transaction.error || new Error("TSC settings storage transaction was aborted."));
    }));
  }

  window.TSCGalleryStore = Object.freeze({
    getAll() {
      return runTransaction("readonly", store => store.getAll()).then(items =>
        items.filter(item => !item.kind || item.kind === "media")
      );
    },
    getNotice() {
      return runTransaction("readonly", store => store.getAll()).then(items =>
        items.find(item => item.kind === "notice") || null
      );
    },
    setNotice(title, message) {
      return openDatabase().then(database => new Promise((resolve, reject) => {
        const transaction = database.transaction(storeName, "readwrite");
        const store = transaction.objectStore(storeName);
        const request = store.getAll();
        let result;

        request.onsuccess = () => {
          const existing = request.result.find(item => item.kind === "notice");
          const notice = {
            ...(existing ? { id: existing.id } : {}),
            kind: "notice",
            title,
            message,
            updatedAt: new Date().toISOString()
          };
          const saveRequest = existing ? store.put(notice) : store.add(notice);
          saveRequest.onsuccess = () => { result = notice; };
          saveRequest.onerror = () => reject(saveRequest.error || new Error("Could not save the TSC notice."));
        };
        request.onerror = () => reject(request.error || new Error("Could not load the current TSC notice."));
        transaction.oncomplete = () => resolve(result);
        transaction.onerror = () => reject(transaction.error || new Error("TSC notice storage failed."));
        transaction.onabort = () => reject(transaction.error || new Error("TSC notice storage was aborted."));
      }));
    },
    getSocialLinks() {
      return runSettingsTransaction("readonly", store => store.get("social-links")).then(settings => settings || null);
    },
    setSocialLinks(links) {
      const settings = { key: "social-links", links, updatedAt: new Date().toISOString() };
      return runSettingsTransaction("readwrite", store => store.put(settings)).then(() => settings);
    },
    clearNotice() {
      return openDatabase().then(database => new Promise((resolve, reject) => {
        const transaction = database.transaction(storeName, "readwrite");
        const store = transaction.objectStore(storeName);
        const request = store.getAll();
        request.onsuccess = () => {
          const existing = request.result.find(item => item.kind === "notice");
          if (existing) store.delete(existing.id);
        };
        request.onerror = () => reject(request.error || new Error("Could not load the current TSC notice."));
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error || new Error("TSC notice removal failed."));
        transaction.onabort = () => reject(transaction.error || new Error("TSC notice removal was aborted."));
      }));
    },
    add(file, section, maximumTotalBytes) {
      return openDatabase().then(database => new Promise((resolve, reject) => {
        const transaction = database.transaction(storeName, "readwrite");
        const store = transaction.objectStore(storeName);
        const request = store.getAll();
        let result;
        let quotaError;

        request.onsuccess = () => {
          const usedBytes = request.result.reduce((total, item) => total + (item.blob?.size || 0), 0);
          if (usedBytes + file.size > maximumTotalBytes) {
            quotaError = new Error("The 5 GB gallery limit would be exceeded. Delete existing media before uploading more.");
            transaction.abort();
            return;
          }
          const addRequest = store.add({
            kind: "media",
            name: file.name,
            type: file.type,
            section,
            blob: file,
            createdAt: new Date().toISOString()
          });
          addRequest.onsuccess = () => { result = addRequest.result; };
        };
        request.onerror = () => reject(request.error || new Error("Could not check gallery storage usage."));
        transaction.oncomplete = () => resolve(result);
        transaction.onerror = () => reject(transaction.error || new Error("Gallery storage transaction failed."));
        transaction.onabort = () => reject(quotaError || transaction.error || new Error("Gallery storage transaction was aborted."));
      }));
    },
    delete(id) {
      return runTransaction("readwrite", store => store.delete(id));
    }
  });
})();
