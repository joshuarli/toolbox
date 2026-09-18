(function () {
  let posts = [];
  let postsById = new Map();
  let postsPromise = null;
  let currentId = null;
  let currentSlide = 0;

  function modal() {
    return document.querySelector("#post-modal");
  }

  function carousel() {
    return document.querySelector("#modal-carousel");
  }

  function postIndex(id) {
    return posts.findIndex((post) => {
      return post.shortid === id;
    });
  }

  function ensurePosts() {
    if (postsPromise) return postsPromise;
    const grid = document.querySelector(".grid[data-posts-src]");
    if (!grid) {
      postsPromise = Promise.resolve([]);
      return postsPromise;
    }
    postsPromise = fetch(grid.dataset.postsSrc)
      .then((response) => {
        if (!response.ok) throw new Error("Could not load post data");
        return response.json();
      })
      .then((data) => {
        posts = data.posts || [];
        postsById = new Map(
          posts.map(function (post) {
            return [post.shortid, post];
          }),
        );
        return posts;
      });
    return postsPromise;
  }

  function syncBackgroundToPost(id) {
    const gridItem = Array.from(document.querySelectorAll("[data-open-modal]")).find(
      function (item) {
        return item.getAttribute("data-open-modal") === id;
      },
    );
    if (!gridItem) return;
    gridItem.scrollIntoView({ block: "center", inline: "nearest" });
  }

  function pauseVideos(root) {
    root.querySelectorAll("video").forEach((video) => {
      video.pause();
    });
  }

  function playActiveVideo(root) {
    root.querySelectorAll("video").forEach((video) => {
      var active = video.closest(".slide.active");
      if (active) {
        video.currentTime = 0;
        video.play().catch(function () {});
      } else {
        video.pause();
      }
    });
  }

  function setActiveSlide(index) {
    const slides = Array.from(carousel().querySelectorAll(".slide"));
    const dots = Array.from(carousel().querySelectorAll(".dot"));
    if (slides.length === 0) return;
    currentSlide = Math.max(0, Math.min(index, slides.length - 1));
    slides.forEach((slide, slideIndex) => {
      slide.classList.toggle("active", slideIndex === currentSlide);
    });
    dots.forEach((dot, dotIndex) => {
      dot.classList.toggle("active", dotIndex === currentSlide);
    });
    playActiveVideo(modal());
  }

  function createMedia(media, index) {
    if (media.type === "video") {
      const video = document.createElement("video");
      video.src = media.src;
      video.muted = true;
      video.loop = true;
      video.playsInline = true;
      video.controls = true;
      video.preload = "metadata";
      return video;
    }

    const img = document.createElement("img");
    img.src = media.src;
    img.alt = `slide ${index + 1}`;
    return img;
  }

  function renderModal(post, slideIndex) {
    const car = carousel();
    car.innerHTML = "";

    post.media.forEach((media, index) => {
      var slide = document.createElement("div");
      slide.className = "slide";
      slide.appendChild(createMedia(media, index));
      car.appendChild(slide);
    });

    if (post.media.length > 1) {
      const prev = document.createElement("button");
      prev.className = "prev";
      prev.type = "button";
      prev.dataset.moveSlide = "-1";
      prev.setAttribute("aria-label", "Previous slide");
      prev.textContent = "\u276E";
      car.append(prev);

      const next = document.createElement("button");
      next.className = "next";
      next.type = "button";
      next.dataset.moveSlide = "1";
      next.setAttribute("aria-label", "Next slide");
      next.textContent = "\u276F";
      car.append(next);

      const dots = document.createElement("div");
      dots.className = "dots";
      post.media.forEach((_media, index) => {
        var dot = document.createElement("button");
        dot.className = "dot";
        dot.type = "button";
        dot.setAttribute("data-go-slide", String(index));
        dot.setAttribute("aria-label", "Slide " + (index + 1));
        dots.appendChild(dot);
      });
      car.append(dots);
    }

    const m = modal();
    const author = m.querySelector(".modal-author");
    const link = m.querySelector(".modal-post-link");
    author.textContent = post.author ? "@" + post.author : "";
    m.querySelector(".modal-meta").textContent = post.meta || "";
    link.href = post.url || "#";
    link.hidden = !post.url;
    m.querySelector(".modal-text").textContent = post.caption || "";
    setActiveSlide(slideIndex || 0);
  }

  function openModal(id, slideIndex) {
    ensurePosts().then(() => {
      var post = postsById.get(id);
      if (!post) return;
      currentId = id;
      syncBackgroundToPost(id);
      renderModal(post, slideIndex || 0);
      modal().classList.add("open");
      modal().setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      playActiveVideo(modal());
    });
  }

  function closeModal() {
    const m = modal();
    if (!m) return;
    pauseVideos(m);
    m.classList.remove("open");
    m.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    currentId = null;
    currentSlide = 0;
  }

  function moveSlide(dir) {
    const slides = carousel().querySelectorAll(".slide");
    if (slides.length === 0) return false;
    if ((dir < 0 && currentSlide === 0) || (dir > 0 && currentSlide === slides.length - 1))
      return false;
    setActiveSlide(currentSlide + dir);
    return true;
  }

  function goSlide(index) {
    setActiveSlide(index);
  }

  function lastSlideIndex(post) {
    return Math.max(0, post.media.length - 1);
  }

  function movePost(dir) {
    if (!currentId) return;
    const cur = postIndex(currentId);
    const nextIndex = cur + dir;
    if (cur === -1 || nextIndex < 0 || nextIndex >= posts.length) return;
    const post = posts[nextIndex];
    openModal(post.shortid, dir < 0 ? lastSlideIndex(post) : 0);
  }

  document.addEventListener("click", (event) => {
    var target = event.target;
    if (!(target instanceof Element)) return;

    var openButton = target.closest("[data-open-modal]");
    if (openButton) {
      event.preventDefault();
      openModal(openButton.getAttribute("data-open-modal"), 0);
      return;
    }

    if (target.closest("[data-close-modal]")) {
      closeModal();
      return;
    }

    var moveButton = target.closest("[data-move-slide]");
    if (moveButton) {
      var dir = Number(moveButton.getAttribute("data-move-slide"));
      if (!moveSlide(dir)) movePost(dir);
      return;
    }

    var dotButton = target.closest("[data-go-slide]");
    if (dotButton) {
      goSlide(Number(dotButton.getAttribute("data-go-slide")));
    }
  });

  document.addEventListener("keydown", (event) => {
    if (!modal() || !modal().classList.contains("open")) return;
    if (event.key === "Escape") closeModal();
    if (event.key === "ArrowLeft" && !moveSlide(-1)) movePost(-1);
    if (event.key === "ArrowRight" && !moveSlide(1)) movePost(1);
  });
})();
