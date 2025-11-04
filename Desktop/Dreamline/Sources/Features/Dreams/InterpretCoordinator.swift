import Foundation

@MainActor
struct InterpretCoordinator {
    let oracle: OracleClient
    
    func runInterpret(dreamText: String) async -> OracleInterpretation? {
        do {
            let extraction = try await oracle.extract(from: dreamText)
            let transit = await AstroService.shared.transits(for: Date())
            let hist = await HistoryService.shared.summarize(days: 30)
            
            return try await oracle.interpret(dreamText: dreamText, extraction: extraction, transit: transit, history: hist)
        } catch {
            return nil
        }
    }
}

