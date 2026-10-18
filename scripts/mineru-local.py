"""Local MinerU entry point with a guard for subpixel OCR crop boxes."""
import runpy
import docvortex.image


def safe_warp(original):
    def warp(image, matrix, size, **kwargs):
        # OCR sometimes rounds a subpixel text box down to zero pixels.
        # Keep the box as a one-pixel sample rather than failing the whole PDF.
        width, height = size
        if width < 0 or height < 0:
            raise ValueError('Negative OCR crop size')
        return original(image, matrix, (max(1, width), max(1, height)), **kwargs)
    return warp


if __name__ == '__main__':
    docvortex.image.warp_image = safe_warp(docvortex.image.warp_image)
    runpy.run_module('mineru.kit.main', run_name='__main__')
