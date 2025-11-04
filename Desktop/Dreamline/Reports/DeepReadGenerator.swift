import Foundation

#if canImport(UIKit)
import UIKit
import PDFKit
#endif

struct DeepReadGenerator {
    static func generate(for dream: DreamEntry) -> URL? {
        #if canImport(UIKit)
        let pdfMetaData = [
            kCGPDFContextCreator: "Dreamline AI",
            kCGPDFContextAuthor: "Dreamline",
            kCGPDFContextTitle: "Deep Read: \(dream.createdAt.formatted(date: .abbreviated, time: .omitted))"
        ]
        let format = UIGraphicsPDFRendererFormat()
        format.documentInfo = pdfMetaData as [String: Any]
        
        let pageWidth = 8.5 * 72.0
        let pageHeight = 11 * 72.0
        let pageRect = CGRect(x: 0, y: 0, width: pageWidth, height: pageHeight)
        
        let renderer = UIGraphicsPDFRenderer(bounds: pageRect, format: format)
        
        let data = renderer.pdfData { context in
            context.beginPage()
            
            let attributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.boldSystemFont(ofSize: 24)
            ]
            let title = "Deep Read Analysis"
            title.draw(at: CGPoint(x: 72, y: 72), withAttributes: attributes)
            
            let bodyAttrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 12)
            ]
            
            var yPos: CGFloat = 120
            let text = dream.rawText
            let lines = text.components(separatedBy: .newlines)
            
            for line in lines.prefix(30) {
                if yPos > pageHeight - 72 {
                    context.beginPage()
                    yPos = 72
                }
                line.draw(at: CGPoint(x: 72, y: yPos), withAttributes: bodyAttrs)
                yPos += 18
            }
            
            if let summary = dream.oracleSummary {
                yPos += 20
                if yPos > pageHeight - 100 {
                    context.beginPage()
                    yPos = 72
                }
                "Oracle Summary".draw(at: CGPoint(x: 72, y: yPos), withAttributes: bodyAttrs)
                yPos += 20
                summary.draw(at: CGPoint(x: 72, y: yPos), withAttributes: bodyAttrs)
            }
        }
        
        let tempDir = FileManager.default.temporaryDirectory
        let filename = "deepread_\(dream.id).pdf"
        let fileURL = tempDir.appendingPathComponent(filename)
        
        do {
            try data.write(to: fileURL)
            return fileURL
        } catch {
            print("Failed to write PDF: \(error)")
            return nil
        }
        #else
        return nil
        #endif
    }
}
