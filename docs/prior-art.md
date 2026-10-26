# Prior art

Researched 25 October 2026 against: native Linux UI, interactive form filling,
arbitrary text boxes, freehand drawing, ticks/crosses, multiple pages, and a
simple export that adds `-signed` and a timestamp before the extension.

| App | Relevant capabilities | Fit for this workflow |
| --- | --- | --- |
| [Okular](https://docs.kde.org/stable_kf6/en/okular/okular/forms.html) | Native Linux viewer with interactive forms. Its [annotation tools](https://docs.kde.org/stable_kf6/en/okular/okular/annotations.html) include typewriter, inline notes, freehand lines, and stamps. | Closest established free option. A configurable document viewer rather than a dedicated fill/sign workflow. Custom tick/cross stamps require setup; automatic timestamped signed filenames were not documented in the reviewed pages. |
| [Xournal++](https://xournalpp.github.io/guide/pdfs/) | Native handwriting/annotation app; PDF pages become journal backgrounds and can be exported. | Strong for drawing and overlay text. Its export documentation explicitly says built-in PDF annotations, including forms, are not preserved. Not a complete interactive form-filling solution. |
| [PDF Sign & Fill](https://github.com/science/pdf-sign-and-fill) | Small PyQt6/PyMuPDF Linux utility with AcroForm filling, text/image stamps, multiple pages, movement and undo. AGPL-3.0. | A close model for a focused native utility. Documentation describes text/image stamps, rather than direct freehand drawing or dedicated tick/cross tools. Uses Save As; the required generated filename was not documented. Extending it would be a reasonable alternative to this implementation. |
| [Master PDF Editor](https://code-industry.net/free-pdf-editor/) | Native Qt Linux editor; forms, annotation, text/image editing, and digital signing. | Broad commercial editor. The vendor limits unregistered use to personal noncommercial purposes and reserves some features for the paid edition. The timestamped export workflow was not documented on the reviewed page. |
| [DeskFolio](https://github.com/imjagdeep/DeskFolio) | Linux desktop app with forms, text, check marks, drawn/typed/image signatures, freehand annotations, and page tools. | Broad feature overlap. Built with React/Tauri, using a web interface inside a desktop shell rather than Qt/GTK widgets. Its documented feature list does not establish the exact cross-mark and timestamped export workflow. |

This was a documentation and source-level comparison, not a hands-on compatibility
test of every app. Missing documentation is not proof that a feature is absent.

For a ready-made app, Okular is the strongest starting point. PDF Marker was
built to provide the requested small, native-widget workflow directly, including
dedicated marks and consistent export naming. Its code is a new implementation;
no source was copied from the projects above.
