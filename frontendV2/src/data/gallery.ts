export interface GalleryItem {
  src: string
  name: string
  meta: string
}

/** Empty on purpose: images come from the attachment backend. */
export const galleryItems: GalleryItem[] = []
